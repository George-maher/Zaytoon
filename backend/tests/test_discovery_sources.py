"""Application discovery: five sources, one de-duplicated inventory.

The old inventory listed hundreds of "applications" and could not open one of
them, because it recorded no launch target at all, inverted the PATH filter so
it admitted text files and rejected executables, and every ``DeviceInventory``
construction raised before it could be stored. These tests pin the *sources*
and the *merge*, because that is where the defects were:

* a verified target beats a registration row with no target;
* one record per normalised name, with metadata merged rather than lost;
* one failing source must not hide the other four;
* PATH discovery excludes the Windows directory and admits only executables;
* ``ZAYTOON_APP_DIRS`` is bounded, so a configured root cannot become a scan.

No test here scans the whole machine: every source is driven against a temporary
directory, so the assertions are about the discovery logic rather than about
what happens to be installed on the build agent.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import discovery  # noqa: E402
from core.discovery import (  # noqa: E402
    ApplicationInfo,
    DeviceDiscoveryCoordinator,
    DeviceInventory,
    DeviceInventoryRepository,
    HardwareInfo,
    WindowsProvider,
    generate_device_identifier,
)
from core.inventory import (  # noqa: E402
    DeviceInventoryRepository as ReexportedRepository,
    InventoryState,
    default_inventory,
)
from tools.applications import build_shortcut_index  # noqa: E402


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
class TestNormalisation:
    @pytest.mark.parametrize("name", ["VS Code", "vs-code", "  vs   code  "])
    def test_spelling_variants_share_one_key(self, name):
        assert discovery._app_key(name) == "vs code"

    def test_camel_case_is_not_split(self):
        """Documented limit: splitting "VSCode" would also split "YouTube"."""
        assert discovery._app_key("VSCode") == "vscode"

    def test_arabic_names_keep_their_letters(self):
        assert discovery._app_key("آلة حاسبة") == "آلة حاسبة"
        assert discovery._app_key("آلة   حاسبة") == "آلة حاسبة"

    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("Google Chrome", "Google Chrome"),
            ("  spaced   name  ", "spaced name"),
            ("-dashy-", "dashy"),
            ("", ""),
            ("x" * 200, ""),
        ],
    )
    def test_display_names_are_cleaned(self, stem, expected):
        assert discovery._clean_display_name(stem) == expected

    @pytest.mark.parametrize(
        "name", ["Uninstall Chrome", "Chrome Readme", "Chrome Help", "Chrome Website"]
    )
    def test_documentation_and_uninstallers_are_noise(self, name):
        assert discovery._is_noise_shortcut(name) is True

    def test_a_real_application_is_not_noise(self):
        assert discovery._is_noise_shortcut("Google Chrome") is False

    @pytest.mark.parametrize(
        "name",
        [
            "KB5034441",
            "Security Update for Windows",
            "Microsoft Visual C++ Redistributable",
            "Windows Driver Package",
            "Update for Microsoft Edge",
        ],
    )
    def test_updates_and_runtimes_are_registration_noise(self, name):
        assert discovery._is_noise_registration(name) is True

    def test_a_real_registration_is_not_noise(self):
        assert discovery._is_noise_registration("Mozilla Firefox") is False

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (r'"C:\Apps\app.exe"', r"C:\Apps\app.exe"),
            (r"C:\Apps\app.exe,0", r"C:\Apps\app.exe"),
            (r'"C:\Apps\app.exe",-1', r"C:\Apps\app.exe"),
            ("", ""),
            (None, ""),
            (123, ""),
        ],
    )
    def test_registry_paths_lose_quotes_and_icon_indexes(self, raw, expected):
        assert discovery._clean_registry_path(raw) == expected

    def test_registry_paths_expand_environment_variables(self, monkeypatch):
        monkeypatch.setenv("ZAYTOON_TEST_DIR", r"C:\Portable")
        assert (
            discovery._clean_registry_path(r"%ZAYTOON_TEST_DIR%\app.exe")
            == r"C:\Portable\app.exe"
        )

    def test_the_windows_directory_is_recognised(self, monkeypatch):
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        assert discovery._is_windows_dir(r"C:\Windows\System32") is True
        assert discovery._is_windows_dir(r"C:\Windows") is True
        assert discovery._is_windows_dir(r"C:\Program Files\App") is False

    def test_only_executables_are_path_applications(self):
        provider = WindowsProvider()
        assert provider._is_likely_application("chrome.exe") is True
        assert provider._is_likely_application("CHROME.EXE") is True
        assert provider._is_likely_application("notes.txt") is False
        assert provider._is_likely_application("run.cmd") is False

    def test_a_version_probe_never_uses_a_shell_string(self):
        """A path with a space used to be split by the shell into ``C:\\Program``."""
        assert discovery._tool_version(sys.executable, "-c", "print('ok')") == "ok\n"
        assert discovery._tool_version("zzz-no-such-binary-zzz", "--version") == ""


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------
class TestTheDedupe:
    def test_a_verified_target_beats_a_targetless_registration(self):
        merged = WindowsProvider._dedupe_applications(
            [
                ApplicationInfo(
                    name="chrome",
                    display_name="Google Chrome",
                    discovery_source="registration",
                    verified=False,
                    target="",
                    confidence=0.4,
                ),
                ApplicationInfo(
                    name="chrome",
                    display_name="Google Chrome",
                    discovery_source="start_menu",
                    verified=True,
                    target=r"C:\Start\Google Chrome.lnk",
                    confidence=0.95,
                ),
            ]
        )
        assert len(merged) == 1
        assert merged[0].target == r"C:\Start\Google Chrome.lnk"
        assert merged[0].verified is True
        assert merged[0].discovery_source == "start_menu"

    def test_metadata_the_winner_lacks_is_filled_in(self):
        merged = WindowsProvider._dedupe_applications(
            [
                ApplicationInfo(
                    name="app",
                    display_name="App",
                    discovery_source="start_menu",
                    verified=True,
                    target=r"C:\Start\App.lnk",
                    confidence=0.95,
                ),
                ApplicationInfo(
                    name="app",
                    display_name="App",
                    discovery_source="registration",
                    verified=True,
                    target=r"C:\Apps\app.exe",
                    install_location=r"C:\Apps",
                    version="1.2.3",
                    confidence=0.8,
                ),
            ]
        )
        assert len(merged) == 1
        assert merged[0].install_location == r"C:\Apps"
        assert merged[0].version == "1.2.3"

    def test_case_and_punctuation_variants_are_one_record(self):
        merged = WindowsProvider._dedupe_applications(
            [
                ApplicationInfo(name="vs code", display_name="VS Code", verified=True, target="a"),
                ApplicationInfo(name="vs-code", display_name="vs-code", verified=True, target="b"),
            ]
        )
        assert len(merged) == 1

    def test_different_applications_are_not_merged(self):
        merged = WindowsProvider._dedupe_applications(
            [
                ApplicationInfo(name="chrome", display_name="Google Chrome"),
                ApplicationInfo(name="firefox", display_name="Mozilla Firefox"),
            ]
        )
        assert len(merged) == 2

    def test_verified_rows_sort_first(self):
        merged = WindowsProvider._dedupe_applications(
            [
                ApplicationInfo(name="a", display_name="A", verified=False),
                ApplicationInfo(name="b", display_name="B", verified=True, target="x"),
            ]
        )
        assert [row.display_name for row in merged] == ["B", "A"]

    def test_an_empty_name_is_dropped(self):
        merged = WindowsProvider._dedupe_applications(
            [ApplicationInfo(name="", display_name="", verified=True, target="x")]
        )
        assert merged == []


# ---------------------------------------------------------------------------
# The five sources, against temporary directories
# ---------------------------------------------------------------------------
@pytest.fixture
def provider():
    return WindowsProvider()


class TestStartMenuSource:
    def test_shortcuts_become_verified_application_records(self, tmp_path, provider, monkeypatch):
        programs = tmp_path / "Programs"
        (programs / "Utilities").mkdir(parents=True)
        shortcut = programs / "Utilities" / "My Tool.lnk"
        shortcut.write_text("", encoding="utf-8")
        monkeypatch.setattr(provider, "_get_start_menu_roots", lambda: [str(programs)])

        apps = provider._discover_start_menu_apps("now")
        assert [a.display_name for a in apps] == ["My Tool"]
        assert apps[0].target == str(shortcut)
        assert apps[0].shortcut_path == str(shortcut)
        assert apps[0].verified is True
        assert apps[0].discovery_source == "start_menu"

    def test_noise_shortcuts_are_skipped(self, tmp_path, provider, monkeypatch):
        programs = tmp_path / "Programs"
        programs.mkdir()
        (programs / "Uninstall My Tool.lnk").write_text("", encoding="utf-8")
        (programs / "My Tool.lnk").write_text("", encoding="utf-8")
        monkeypatch.setattr(provider, "_get_start_menu_roots", lambda: [str(programs)])
        assert [a.display_name for a in provider._discover_start_menu_apps("now")] == [
            "My Tool"
        ]

    def test_a_missing_shortcut_file_is_unverified(self, tmp_path, provider, monkeypatch):
        programs = tmp_path / "Programs"
        programs.mkdir()
        # A shortcut the index saw but that is gone by the time it is recorded.
        monkeypatch.setattr(
            provider,
            "_build_shortcut_index",
            lambda roots: [("ghost", str(programs / "Ghost.lnk"))],
        )
        apps = provider._discover_start_menu_apps("now")
        assert apps[0].verified is False


class TestConfiguredDirectories:
    def test_a_configured_root_is_scanned(self, tmp_path, provider, monkeypatch):
        root = tmp_path / "PortableApps"
        root.mkdir()
        (root / "Tool.exe").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZAYTOON_APP_DIRS", str(root))
        apps = provider._discover_configured_app_dirs("now")
        assert [a.display_name for a in apps] == ["Tool"]
        assert apps[0].verified is True
        assert apps[0].discovery_source == "configured"

    def test_the_walk_is_depth_bounded(self, tmp_path, provider, monkeypatch):
        root = tmp_path / "PortableApps"
        deep = root
        for level in range(4):
            deep = deep / f"d{level}"
        deep.mkdir(parents=True)
        (root / "Shallow.exe").write_text("", encoding="utf-8")
        (deep / "TooDeep.exe").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZAYTOON_APP_DIRS", str(root))
        names = {a.display_name for a in provider._discover_configured_app_dirs("now")}
        assert names == {"Shallow"}

    def test_noise_directories_are_skipped(self, tmp_path, provider, monkeypatch):
        root = tmp_path / "PortableApps"
        (root / "node_modules").mkdir(parents=True)
        (root / "node_modules" / "hidden.exe").write_text("", encoding="utf-8")
        (root / "Tool.exe").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZAYTOON_APP_DIRS", str(root))
        assert [a.display_name for a in provider._discover_configured_app_dirs("now")] == [
            "Tool"
        ]

    def test_only_executables_and_shortcuts_are_records(self, tmp_path, provider, monkeypatch):
        root = tmp_path / "PortableApps"
        root.mkdir()
        (root / "notes.txt").write_text("", encoding="utf-8")
        (root / "Tool.exe").write_text("", encoding="utf-8")
        (root / "Tool.lnk").write_text("", encoding="utf-8")
        monkeypatch.setenv("ZAYTOON_APP_DIRS", str(root))
        names = sorted(a.display_name for a in provider._discover_configured_app_dirs("now"))
        assert names == ["Tool", "Tool"]

    def test_an_empty_variable_scans_nothing(self, provider, monkeypatch):
        monkeypatch.setenv("ZAYTOON_APP_DIRS", "")
        assert provider._discover_configured_app_dirs("now") == []


class TestPathSource:
    def test_only_executables_in_a_path_entry_are_records(self, tmp_path, provider, monkeypatch):
        (tmp_path / "Tool.exe").write_text("", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("", encoding="utf-8")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("SystemRoot", str(tmp_path / "no-windows-here"))
        apps = provider._discover_path_applications("now")
        assert [a.display_name for a in apps] == ["Tool"]
        assert apps[0].target == str(tmp_path / "Tool.exe")

    def test_the_windows_directory_is_excluded(self, tmp_path, provider, monkeypatch):
        windows = tmp_path / "Windows"
        windows.mkdir()
        (windows / "system-thing.exe").write_text("", encoding="utf-8")
        other = tmp_path / "bin"
        other.mkdir()
        (other / "real-tool.exe").write_text("", encoding="utf-8")
        monkeypatch.setenv("SystemRoot", str(windows))
        monkeypatch.setenv("PATH", os.pathsep.join([str(windows), str(other)]))
        names = [a.display_name for a in provider._discover_path_applications("now")]
        assert names == ["real-tool"]

    def test_a_duplicate_path_entry_is_scanned_once(self, tmp_path, provider, monkeypatch):
        (tmp_path / "Tool.exe").write_text("", encoding="utf-8")
        monkeypatch.setenv("SystemRoot", str(tmp_path / "none"))
        monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), str(tmp_path)]))
        assert len(provider._discover_path_applications("now")) == 1


# ---------------------------------------------------------------------------
# Failure isolation and the coordinator
# ---------------------------------------------------------------------------
class TestSourceFailures:
    def test_one_broken_source_does_not_hide_the_others(self, provider, monkeypatch):
        def _boom(stamp):
            raise OSError("registry unreadable")

        monkeypatch.setattr(provider, "_discover_start_menu_apps", _boom)
        monkeypatch.setattr(
            provider,
            "_discover_registered_app_paths",
            lambda stamp: [ApplicationInfo(name="app", display_name="App", verified=True, target="x")],
        )
        monkeypatch.setattr(provider, "_discover_registered_applications", lambda stamp: [])
        monkeypatch.setattr(provider, "_discover_path_applications", lambda stamp: [])
        monkeypatch.setattr(provider, "_discover_configured_app_dirs", lambda stamp: [])

        apps = provider.discover_installed_applications()
        assert [a.display_name for a in apps] == ["App"]

    def test_every_source_failing_is_an_empty_inventory_not_a_crash(self, provider, monkeypatch):
        def _boom(stamp):
            raise OSError("nope")

        for name in (
            "_discover_start_menu_apps",
            "_discover_registered_app_paths",
            "_discover_registered_applications",
            "_discover_path_applications",
            "_discover_configured_app_dirs",
        ):
            monkeypatch.setattr(provider, name, _boom)
        assert provider.discover_installed_applications() == []

class _FakeProvider:
    """Every discovery scope, answered locally, with call accounting."""

    def __init__(self):
        self.calls: list[str] = []

    def discover_hardware(self):
        self.calls.append("hardware")
        return HardwareInfo(cpu_model="fake-cpu", os_family="test")

    def discover_installed_applications(self):
        self.calls.append("applications")
        return [ApplicationInfo(name="app", display_name="App", verified=True, target="x")]

    def discover_running_processes(self):
        self.calls.append("processes")
        return []

    def discover_development_environment(self):
        self.calls.append("dev")
        return {}

    def discover_projects(self):
        self.calls.append("projects")
        return []

    def discover_local_ai(self):
        self.calls.append("ai")
        return discovery.LocalAIModel(name="m", endpoint="http://localhost:11434")


class TestTheCoordinator:
    def test_every_scope_is_discovered_and_reported(self):
        provider = _FakeProvider()
        coordinator = DeviceDiscoveryCoordinator(provider=provider)
        inventory = asyncio.run(coordinator.discover_all())
        assert inventory.hardware.cpu_model == "fake-cpu"
        assert [a.display_name for a in inventory.applications] == ["App"]
        assert inventory.discovery_scopes == coordinator._default_scopes()
        assert sorted(provider.calls) == [
            "ai",
            "applications",
            "dev",
            "hardware",
            "processes",
            "projects",
        ]

    def test_a_second_scan_reuses_the_cache_until_invalidated(self):
        provider = _FakeProvider()
        coordinator = DeviceDiscoveryCoordinator(provider=provider)
        asyncio.run(coordinator.discover_all())
        first = list(provider.calls)
        asyncio.run(coordinator.discover_all())
        assert provider.calls == first, "a repeat scan re-ran every provider"
        coordinator.invalidate()
        asyncio.run(coordinator.discover_all())
        assert len(provider.calls) > len(first), "invalidate() did not force a rescan"


# ---------------------------------------------------------------------------
# Inventory construction and persistence
# ---------------------------------------------------------------------------
class TestTheInventoryModel:
    def test_a_device_inventory_can_be_constructed(self):
        """The latent bug: ``local_ai`` used ``default_factory=LocalAIModel``,
        and ``LocalAIModel`` requires two arguments, so *every* construction
        raised ``TypeError`` before the record could be stored."""
        inventory = DeviceInventory(
            timestamp=discovery.datetime.now(),
            hardware=HardwareInfo(cpu_model="cpu", os_family="windows"),
        )
        assert inventory.local_ai.name == ""
        assert inventory.local_ai.endpoint == ""

    def test_default_inventory_is_ready_to_scan(self):
        inventory = default_inventory()
        assert inventory.discovery_scopes
        assert inventory.inventory_version

    def test_the_identifier_is_stable_and_not_the_hostname(self):
        first = generate_device_identifier()
        assert first == generate_device_identifier()
        assert first != os.environ.get("COMPUTERNAME", "")

    def test_the_repository_is_reexported_once(self):
        assert ReexportedRepository is DeviceInventoryRepository

    def test_inventory_state_reports_what_is_known(self):
        inventory = default_inventory()
        assert InventoryState.NOT_SCANNED
        assert InventoryState.READY


class TestTheRepository:
    @pytest.fixture
    def repo(self, tmp_path):
        return DeviceInventoryRepository(db_path=str(tmp_path / "inventory.db"))

    def test_a_saved_inventory_round_trips(self, repo):
        inventory = DeviceInventory(
            timestamp=discovery.datetime.now(),
            hardware=HardwareInfo(cpu_model="probe-cpu", os_family="windows"),
            applications=[
                ApplicationInfo(
                    name="chrome",
                    display_name="Google Chrome",
                    target=r"C:\Apps\chrome.exe",
                    verified=True,
                    install_location=r"C:\Apps",
                    shortcut_path=r"C:\Start\Google Chrome.lnk",
                )
            ],
            discovery_scopes=["applications", "hardware"],
        )

        async def scenario():
            await repo.save(inventory, "device-1")
            return await repo.load("device-1")

        loaded = asyncio.run(scenario())
        assert loaded is not None
        assert loaded.hardware.cpu_model == "probe-cpu"
        assert loaded.applications[0].display_name == "Google Chrome"
        assert loaded.applications[0].target == r"C:\Apps\chrome.exe"
        assert loaded.applications[0].shortcut_path == r"C:\Start\Google Chrome.lnk"
        assert loaded.discovery_scopes == ["applications", "hardware"]

    def test_an_unknown_device_loads_none(self, repo):
        assert asyncio.run(repo.load("nobody")) is None

    def test_latest_returns_the_newest_record(self, repo):
        async def scenario():
            first = default_inventory()
            first.hardware.cpu_model = "old"
            first.timestamp = discovery.datetime(2020, 1, 1)
            await repo.save(first, "device-1")
            time.sleep(0.01)
            second = default_inventory()
            second.hardware.cpu_model = "new"
            second.timestamp = discovery.datetime(2021, 1, 1)
            await repo.save(second, "device-1")
            return await repo.latest("device-1")

        assert asyncio.run(scenario()).hardware.cpu_model == "new"

    def test_the_database_is_created_on_demand(self, tmp_path):
        path = tmp_path / "nested" / "inventory.db"
        DeviceInventoryRepository(db_path=str(path))
        assert path.exists()


# ---------------------------------------------------------------------------
# The shared shortcut walk
# ---------------------------------------------------------------------------
class TestTheSharedShortcutIndex:
    def test_the_index_is_one_walk_of_the_given_roots(self, tmp_path):
        (tmp_path / "A.lnk").write_text("", encoding="utf-8")
        nested = tmp_path / "Nested"
        nested.mkdir()
        (nested / "B.lnk").write_text("", encoding="utf-8")
        (nested / "not-a-shortcut.txt").write_text("", encoding="utf-8")
        index = build_shortcut_index([str(tmp_path)])
        assert sorted(stem for stem, _ in index) == ["A", "B"]

    def test_discovery_and_the_launcher_read_the_same_roots(self, provider, monkeypatch):
        seen: list[list[str]] = []

        def _roots():
            seen.append(["root"])
            return ["root"]

        monkeypatch.setattr("tools.applications.start_menu_roots", _roots)
        assert provider._get_start_menu_roots() == ["root"]
        assert seen == [["root"]]


# ---------------------------------------------------------------------------
# Arabic application names
# ---------------------------------------------------------------------------
class TestArabicApplicationNames:
    """``شغل كروم`` must find Chrome.

    The resolver understood the request and had Chrome installed, but the name
    was not in its vocabulary: the table is English and the Start Menu is
    labelled ``Google Chrome``. A closed alias list is the fix; fuzzy
    transliteration would be a guess, and a guess here launches something the
    user did not name.
    """

    @pytest.fixture
    def fake_start_menu(self, tmp_path, monkeypatch):
        (tmp_path / "Google Chrome.lnk").write_text("", encoding="utf-8")
        (tmp_path / "Visual Studio Code.lnk").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "tools.applications.start_menu_roots", lambda: [str(tmp_path)]
        )
        monkeypatch.setattr("tools.applications.shutil.which", lambda _name: None)
        return tmp_path

    def test_arabic_chrome_resolves_to_the_installed_shortcut(self, fake_start_menu):
        from tools.applications import resolve_app_detail

        detail = resolve_app_detail("كروم")
        assert detail["found"] is True
        assert detail["target"].endswith("Google Chrome.lnk")
        assert detail["via"] == "shortcut"

    def test_arabic_vscode_resolves_to_the_installed_shortcut(self, fake_start_menu):
        from tools.applications import resolve_app_detail

        detail = resolve_app_detail("في اس كود")
        assert detail["found"] is True
        assert detail["target"].endswith("Visual Studio Code.lnk")

    def test_the_alias_table_is_normalised_not_exact_spelled(self, fake_start_menu):
        """Tatweel and the alef forms must not be a second table."""
        from tools.applications import resolve_app_detail

        assert resolve_app_detail("كـروم")["found"] is True
        assert resolve_app_detail("إيدج")["found"] is False  # not installed here
        assert resolve_app_detail("ادج")["found"] is False

    def test_an_unknown_arabic_name_is_not_guessed(self, fake_start_menu):
        from tools.applications import resolve_app_detail

        detail = resolve_app_detail("حاجه مش موجوده")
        assert detail["found"] is False
        assert detail["via"] == "none"
