"""The launcher tools and the application launcher they depend on.

The four tools are deliberately split into two kinds, and the split is the
security property this file protects:

* ``find_paths``, ``list_apps`` and ``refresh_inventory`` are **read-only**.
  They stat, list and read the registry; nothing they do starts a process.
* ``open_target`` is the single launch entry point. It resolves through
  ``core.targets`` and hands the verified artifact to ``launch_target``.

Both are ``safe``-tier: reading an inventory is not a privileged act, and the
launch path refuses code-executing suffixes and re-verifies existence at the
moment of launch rather than trusting the scan that produced the target.

These tests use mocked launches (``os.startfile`` and ``OpenUrl.execute`` are
patched). Nothing here opens a real window or a real browser, and the report
says so.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.applications as applications  # noqa: E402
import tools.launcher as launcher  # noqa: E402
from core.discovery import ApplicationInfo  # noqa: E402
from tools.base import ToolResult, ToolValidationError  # noqa: E402
from tools.registry import build_default_registry  # noqa: E402
from tools.sandbox import SecurityError, validate_app_target  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _app(name="App", verified=True, source="start_menu", target="C:/Apps/App.exe"):
    return ApplicationInfo(
        name=name.lower(),
        display_name=name,
        discovery_source=source,
        verified=verified,
        target=target,
    )


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
class TestTheRegistryWiring:
    @pytest.mark.parametrize(
        "name", ["open_target", "find_paths", "list_apps", "refresh_inventory"]
    )
    def test_every_launcher_tool_is_registered(self, registry, name):
        assert registry.has(name), f"{name} is not in the registry"

    @pytest.mark.parametrize(
        "name", ["open_target", "find_paths", "list_apps", "refresh_inventory"]
    )
    def test_reading_and_opening_are_safe_tier(self, registry, name):
        """Read-only discovery is separate from launch, and neither prompts.

        The launch path's own refusals (suffixes, containment, existence) are
        the guard, not a confirmation dialog: prompting per file would train the
        user to click through the one prompt that matters.
        """
        assert registry.effective_tier(name) == "safe"
        assert registry.assess(name).requires_confirmation is False

    def test_the_tools_describe_themselves(self, registry):
        described = {row["name"]: row for row in registry.describe()}
        for name in ("open_target", "find_paths", "list_apps", "refresh_inventory"):
            assert described[name]["permission"] == "safe"
            assert described[name]["description"]
            assert described[name]["schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# open_target: the five answers
# ---------------------------------------------------------------------------
class TestOpenTarget:
    @pytest.fixture(autouse=True)
    def _no_launch(self, monkeypatch):
        """Record launches instead of performing them, for every test here."""
        self.launched: list[str] = []
        self.urls: list[str] = []

        async def _launch(target):
            self.launched.append(target)
            return ToolResult.ok(f"Opened {target}", {"target": target})

        async def _open_url(self_, url, **kwargs):
            self.urls.append(url)
            return ToolResult.ok(f"Opened {url}", {"url": url})

        monkeypatch.setattr(launcher, "launch_target", _launch)
        monkeypatch.setattr("tools.web.OpenUrl.execute", _open_url)

    def test_a_website_is_opened_through_the_url_policy(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "website",
                "site": "YouTube",
                "url": "https://www.youtube.com/",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="YouTube"))
        assert result.success is True
        assert self.urls == ["https://www.youtube.com/"]
        assert self.launched == []
        assert result.data["kind"] == "website"

    def test_an_application_is_launched_by_its_verified_target(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "app",
                "app": "vscode",
                "target": "C:/Start Menu/Visual Studio Code.lnk",
                "via": "shortcut",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="vscode"))
        assert self.launched == ["C:/Start Menu/Visual Studio Code.lnk"]
        assert result.data["kind"] == "app"
        assert result.data["via"] == "shortcut"

    def test_a_file_is_re_checked_then_opened(self, tmp_path, monkeypatch):
        report = tmp_path / "report.pdf"
        report.write_text("x", encoding="utf-8")
        monkeypatch.setattr(launcher, "is_authorized_path", lambda path: True)
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "file",
                "path": str(report),
                "name": "report.pdf",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="report.pdf"))
        assert self.launched == [str(report)]
        assert result.data["kind"] == "file"

    def test_a_folder_is_opened(self, tmp_path, monkeypatch):
        folder = tmp_path / "reports"
        folder.mkdir()
        monkeypatch.setattr(launcher, "is_authorized_path", lambda path: True)
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "folder",
                "path": str(folder),
                "name": "reports",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="reports"))
        assert self.launched == [str(folder)]
        assert result.data["kind"] == "folder"

    def test_an_outside_path_is_refused_before_launching(self, monkeypatch):
        monkeypatch.setattr(launcher, "is_authorized_path", lambda path: False)
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {"kind": "file", "path": "C:/Windows/system.ini"},
        )
        result = _run(launcher.OpenTarget().execute(request="system.ini"))
        assert result.success is False
        assert result.blocked is True
        assert self.launched == []

    def test_a_file_that_vanished_is_an_honest_failure(self, tmp_path, monkeypatch):
        missing = tmp_path / "gone.pdf"
        monkeypatch.setattr(launcher, "is_authorized_path", lambda path: True)
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {"kind": "file", "path": str(missing)},
        )
        result = _run(launcher.OpenTarget().execute(request="gone.pdf"))
        assert result.success is False
        assert "no longer exists" in (result.error or "")
        assert self.launched == []

    def test_a_refused_suffix_is_denied(self, tmp_path, monkeypatch):
        installer = tmp_path / "setup.exe"
        installer.write_text("x", encoding="utf-8")
        monkeypatch.setattr(launcher, "is_authorized_path", lambda path: True)
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {"kind": "file", "path": str(installer)},
        )
        result = _run(launcher.OpenTarget().execute(request="setup.exe"))
        assert result.blocked is True
        assert self.launched == []

    def test_ambiguity_lists_candidates_and_launches_nothing(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "ambiguous",
                "candidates": [
                    {"name": "report-q3.pdf", "path": "C:/Docs/report-q3.pdf", "is_dir": False},
                    {"name": "report-q4.pdf", "path": "C:/Docs/report-q4.pdf", "is_dir": False},
                ],
            },
        )
        result = _run(launcher.OpenTarget().execute(request="report"))
        assert result.success is True
        assert "report-q3.pdf" in result.output
        assert "report-q4.pdf" in result.output
        assert result.data["kind"] == "ambiguous"
        assert self.launched == []

    def test_a_blocked_target_is_denied_with_the_reason(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "blocked",
                "path": "C:/Docs/macro.docm",
                "reason": "Refusing to open 'macro.docm': it can execute code.",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="macro.docm"))
        assert result.blocked is True
        assert "execute code" in (result.error or "")
        assert self.launched == []

    def test_nothing_found_is_a_failure_with_the_resolvers_reason(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "resolve_open_request",
            lambda request, **kw: {
                "kind": "none",
                "reason": "No application, website, file or folder named 'zzz' was found.",
            },
        )
        result = _run(launcher.OpenTarget().execute(request="zzz"))
        assert result.success is False
        assert "zzz" in (result.error or "")
        assert self.launched == []

    def test_an_empty_request_is_rejected_by_validation(self):
        with pytest.raises(ToolValidationError):
            launcher.OpenTarget().validate({"request": "  "})

    def test_the_tool_never_shells_out_with_the_request(self, monkeypatch):
        """The request text reaches a resolver, never a command line."""
        seen = {}

        def _resolve(request, **kw):
            seen["request"] = request
            return {"kind": "none", "reason": "nothing"}

        monkeypatch.setattr(launcher, "resolve_open_request", _resolve)
        _run(launcher.OpenTarget().execute(request="open chrome & whoami"))
        assert seen["request"] == "open chrome & whoami"
        assert self.launched == []


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------
class TestFindPaths:
    def test_results_are_rendered_and_returned(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "search_paths",
            lambda query, **kw: [
                {"path": "C:/Docs/report.pdf", "name": "report.pdf", "is_dir": False, "score": 100},
                {"path": "C:/Docs/reports", "name": "reports", "is_dir": True, "score": 60},
            ],
        )
        result = _run(launcher.FindPaths().execute(query="report"))
        assert result.success is True
        assert result.data["count"] == 2
        assert "report.pdf" in result.output

    def test_kind_filters_the_results(self, monkeypatch):
        monkeypatch.setattr(
            launcher,
            "search_paths",
            lambda query, **kw: [
                {"path": "C:/Docs/report.pdf", "name": "report.pdf", "is_dir": False, "score": 100},
                {"path": "C:/Docs/reports", "name": "reports", "is_dir": True, "score": 60},
            ],
        )
        folders = _run(launcher.FindPaths().execute(query="report", kind="folder"))
        assert [r["name"] for r in folders.data["results"]] == ["reports"]
        files = _run(launcher.FindPaths().execute(query="report", kind="file"))
        assert [r["name"] for r in files.data["results"]] == ["report.pdf"]

    def test_an_unknown_kind_is_a_validation_error(self):
        with pytest.raises(ToolValidationError):
            _run(launcher.FindPaths().execute(query="x", kind="everything"))

    def test_an_empty_query_is_a_validation_error(self):
        with pytest.raises(ToolValidationError):
            launcher.FindPaths().validate({"query": ""})

    def test_the_limit_is_clamped_to_the_documented_range(self, monkeypatch):
        seen = {}

        def _search(query, **kw):
            seen["limit"] = kw.get("limit")
            return []

        monkeypatch.setattr(launcher, "search_paths", _search)
        _run(launcher.FindPaths().execute(query="x", limit=999))
        assert seen["limit"] == 25
        _run(launcher.FindPaths().execute(query="x", limit=0))
        assert seen["limit"] == 1

    def test_no_match_is_a_successful_empty_answer(self, monkeypatch):
        monkeypatch.setattr(launcher, "search_paths", lambda query, **kw: [])
        result = _run(launcher.FindPaths().execute(query="zzz"))
        assert result.success is True
        assert result.data["count"] == 0
        assert "No files named" in result.output

    def test_a_search_failure_is_reported_not_raised(self, monkeypatch):
        def _boom(query, **kw):
            raise OSError("disk gone")

        monkeypatch.setattr(launcher, "search_paths", _boom)
        result = _run(launcher.FindPaths().execute(query="x"))
        assert result.success is False
        assert "disk gone" in (result.error or "")


class TestListApps:
    @pytest.fixture(autouse=True)
    def _apps(self, monkeypatch):
        self.apps = [
            _app("Google Chrome", verified=True, source="start_menu", target="C:/Chrome/chrome.exe"),
            _app("Mystery Tool", verified=False, source="registration", target=""),
        ]

        async def _installed(force=False):
            return list(self.apps)

        monkeypatch.setattr(launcher, "_installed_apps", _installed)

    def test_all_rows_are_listed_with_their_verification(self):
        result = _run(launcher.ListApps().execute())
        assert result.data["count"] == 2
        assert "Google Chrome" in result.output
        assert "registration only" in result.output

    def test_verified_only_hides_registrations(self):
        result = _run(launcher.ListApps().execute(verified_only=True))
        assert [row["name"] for row in result.data["apps"]] == ["Google Chrome"]

    def test_the_query_filter_folds_case(self):
        result = _run(launcher.ListApps().execute(query="CHROME"))
        assert [row["name"] for row in result.data["apps"]] == ["Google Chrome"]

    def test_no_match_is_an_empty_answer_not_an_error(self):
        result = _run(launcher.ListApps().execute(query="zzz"))
        assert result.success is True
        assert result.data["count"] == 0


class TestRefreshInventory:
    def test_a_rescan_forces_the_scan_and_reports_sources(self, monkeypatch):
        calls: list[bool] = []

        async def _installed(force=False):
            calls.append(force)
            return [
                _app("A", source="start_menu"),
                _app("B", source="start_menu"),
                _app("C", verified=False, source="registration", target=""),
            ]

        monkeypatch.setattr(launcher, "_installed_apps", _installed)
        result = _run(launcher.RefreshInventory().execute())
        assert calls == [True]
        assert result.data["total"] == 3
        assert result.data["verified"] == 2
        assert result.data["by_source"] == {"start_menu": 2, "registration": 1}

    def test_a_scan_failure_is_reported(self, monkeypatch):
        async def _boom(force=False):
            raise OSError("registry unreadable")

        monkeypatch.setattr(launcher, "_installed_apps", _boom)
        result = _run(launcher.RefreshInventory().execute())
        assert result.success is False
        assert "registry unreadable" in (result.error or "")


# ---------------------------------------------------------------------------
# open_app and the website fallback
# ---------------------------------------------------------------------------
class TestOpenAppFallsBackToTheWebsite:
    @pytest.fixture(autouse=True)
    def _windows_and_no_launch(self, monkeypatch):
        """Exercise the Windows branch on every platform, with launches recorded.

        ``_IS_WINDOWS`` is the module's own switch; pinning it makes the test
        about the decision tree rather than about which OS CI happens to run.
        """
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)
        self.launched: list[str] = []
        self.urls: list[str] = []

        async def _launch(target):
            self.launched.append(target)
            return ToolResult.ok(f"Opened {target}", {"target": target})

        async def _open_url(self_, url, **kwargs):
            self.urls.append(url)
            return ToolResult.ok(f"Opened {url}", {"url": url})

        monkeypatch.setattr(applications, "launch_target", _launch)
        monkeypatch.setattr("tools.web.OpenUrl.execute", _open_url)

    def test_open_youtube_opens_the_site_when_no_app_is_installed(self, monkeypatch):
        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda name, **kw: {"target": None, "found": False, "via": "none"},
        )
        result = _run(applications.OpenApp().execute(app="youtube"))
        assert result.success is True, result.output
        assert self.urls == ["https://www.youtube.com/"]
        assert result.data["via"] == "website"
        assert self.launched == []

    def test_an_installed_application_is_launched_instead_of_the_site(self, monkeypatch):
        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda name, **kw: {
                "target": "C:/Apps/YouTube.exe",
                "found": True,
                "via": "shortcut",
            },
        )
        result = _run(applications.OpenApp().execute(app="youtube"))
        assert self.launched == ["C:/Apps/YouTube.exe"]
        assert self.urls == []
        assert result.success is True

    def test_an_unknown_name_reports_every_place_it_looked(self, monkeypatch):
        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda name, **kw: {"target": None, "found": False, "via": "none"},
        )
        result = _run(applications.OpenApp().execute(app="zzz-not-an-app"))
        assert result.success is False
        message = result.error or ""
        assert "Start Menu" in message
        assert "PATH" in message
        assert "ZAYTOON_APP_DIRS" in message
        assert self.launched == []

    def test_a_builtin_scheme_is_attempted_last(self, monkeypatch):
        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda name, **kw: {"target": "ms-settings:", "found": False, "via": "builtin"},
        )
        result = _run(applications.OpenApp().execute(app="settings"))
        assert self.launched == ["ms-settings:"]
        assert result.success is True

    def test_a_refused_name_never_reaches_the_resolver(self):
        with pytest.raises(ToolValidationError):
            applications.OpenApp().validate({"app": "chrome & whoami"})


# ---------------------------------------------------------------------------
# Shortcut matching, locator ordering, and launch validation
# ---------------------------------------------------------------------------
class TestShortcutMatching:
    def test_an_exact_name_beats_a_substring(self):
        index = [
            ("chrome apps", r"C:\Start\Chrome Apps.lnk"),
            ("google chrome", r"C:\Start\Google Chrome.lnk"),
        ]
        assert (
            applications._find_start_menu_shortcut("chrome", index=index)
            == r"C:\Start\Google Chrome.lnk"
        )

    def test_a_display_name_matches_an_alias(self):
        """``vscode`` must find ``Visual Studio Code.lnk``."""
        index = [("visual studio code", r"C:\Start\Visual Studio Code.lnk")]
        assert (
            applications._find_start_menu_shortcut("vscode", index=index)
            == r"C:\Start\Visual Studio Code.lnk"
        )

    def test_documentation_and_uninstallers_are_not_the_application(self):
        index = [
            ("uninstall chrome", r"C:\Start\Uninstall Chrome.lnk"),
            ("chrome help", r"C:\Start\Chrome Help.lnk"),
        ]
        assert applications._find_start_menu_shortcut("chrome", index=index) is None

    def test_an_unrelated_shortcut_does_not_match(self):
        index = [("notepad", r"C:\Start\Notepad.lnk")]
        assert applications._find_start_menu_shortcut("chrome", index=index) is None


class TestTheLocator:
    def test_a_launchable_executable_wins(self, monkeypatch):
        monkeypatch.setattr(applications.shutil, "which", lambda name: r"C:\bin\chrome.exe")
        monkeypatch.setattr(applications, "_find_start_menu_shortcut", lambda name, **kw: None)
        assert applications._locate("chrome") == (r"C:\bin\chrome.exe", "path")

    def test_a_blocked_path_script_loses_to_a_start_menu_shortcut(self, monkeypatch):
        """``vscode`` resolves on PATH to ``code.CMD``, which is refused at launch.

        Returning the blocked script because it was found first meant the
        resolver confidently handed back the one target it would then refuse.
        """
        monkeypatch.setattr(applications.shutil, "which", lambda name: r"C:\bin\code.CMD")
        monkeypatch.setattr(
            applications,
            "_find_start_menu_shortcut",
            lambda name, **kw: r"C:\Start\Visual Studio Code.lnk",
        )
        assert applications._locate("vscode") == (
            r"C:\Start\Visual Studio Code.lnk",
            "shortcut",
        )

    def test_a_blocked_script_is_still_evidence_when_nothing_else_exists(self, monkeypatch):
        monkeypatch.setattr(applications.shutil, "which", lambda name: r"C:\bin\code.CMD")
        monkeypatch.setattr(applications, "_find_start_menu_shortcut", lambda name, **kw: None)
        assert applications._locate("vscode") == (r"C:\bin\code.CMD", "path")

    def test_nothing_found_is_none(self, monkeypatch):
        monkeypatch.setattr(applications.shutil, "which", lambda name: None)
        monkeypatch.setattr(applications, "_find_start_menu_shortcut", lambda name, **kw: None)
        assert applications._locate("zzz") is None


class TestLaunchValidation:
    def test_a_shortcut_outside_the_start_menu_is_refused(self, tmp_path):
        lnk = tmp_path / "App.lnk"
        lnk.write_text("", encoding="utf-8")
        with pytest.raises(SecurityError):
            applications.validate_launch_target(str(lnk))

    def test_a_shortcut_inside_a_start_menu_root_is_allowed(self, tmp_path, monkeypatch):
        programs = tmp_path / "Programs"
        programs.mkdir()
        lnk = programs / "App.lnk"
        lnk.write_text("", encoding="utf-8")
        monkeypatch.setattr(applications, "start_menu_roots", lambda: [str(programs)])
        assert applications.validate_launch_target(str(lnk)) == str(lnk)

    def test_the_general_app_gate_still_refuses_every_shortcut(self, tmp_path):
        """The narrow Start-Menu exception must not weaken ``validate_app_target``."""
        lnk = tmp_path / "App.lnk"
        lnk.write_text("", encoding="utf-8")
        with pytest.raises(SecurityError):
            validate_app_target(str(lnk))

    @pytest.mark.parametrize("name", ["run.bat", "run.cmd", "run.ps1", "run.vbs"])
    def test_a_script_is_refused(self, tmp_path, name):
        script = tmp_path / name
        script.write_text("", encoding="utf-8")
        with pytest.raises(SecurityError):
            applications.validate_launch_target(str(script))

    def test_shell_syntax_in_a_target_is_refused(self):
        with pytest.raises(SecurityError):
            applications.validate_launch_target(r"C:\Apps\a&calc.exe")

    def test_an_empty_target_is_refused(self):
        with pytest.raises(SecurityError):
            applications.validate_launch_target("   ")

    def test_a_missing_executable_is_refused_now(self, tmp_path):
        with pytest.raises(SecurityError) as info:
            applications.validate_launch_target(str(tmp_path / "gone.exe"))
        assert "no longer installed" in str(info.value)


class TestLaunchTarget:
    def test_the_checked_target_reaches_the_shell_launcher(self, tmp_path, monkeypatch):
        exe = tmp_path / "app.exe"
        exe.write_text("", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)
        monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)

        result = _run(applications.launch_target(str(exe)))
        assert result.success is True
        assert opened == [str(exe)]

    def test_a_uri_scheme_is_handed_to_the_launcher(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)
        monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
        result = _run(applications.launch_target("ms-settings:"))
        assert result.success is True
        assert opened == ["ms-settings:"]

    def test_a_missing_target_fails_instead_of_launching(self, tmp_path, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)
        monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
        result = _run(applications.launch_target(str(tmp_path / "gone.exe")))
        assert result.success is False
        assert opened == []

    def test_a_refused_target_is_denied_and_never_launched(self, tmp_path, monkeypatch):
        script = tmp_path / "run.bat"
        script.write_text("", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)
        monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
        result = _run(applications.launch_target(str(script)))
        assert result.blocked is True
        assert opened == []

    def test_a_launcher_error_is_reported_not_raised(self, tmp_path, monkeypatch):
        exe = tmp_path / "app.exe"
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(applications, "_IS_WINDOWS", True)

        def _boom(path):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", _boom, raising=False)
        result = _run(applications.launch_target(str(exe)))
        assert result.success is False
        assert "no association" in (result.error or "")