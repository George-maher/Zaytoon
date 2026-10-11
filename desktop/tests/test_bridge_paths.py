"""The desktop bridge must not be a way to run anything the page names.

`open_path` and `reveal_in_os` end in `os.startfile` / `open` / `xdg-open`, and all
three of those *execute* a file the OS considers a program. These tests pin the
policy in `app/paths.py` — granted roots, symlinks resolved before containment, no
runnable types — and check that the bridge actually consults it instead of
reaching for the OS directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import bridge as bridge_mod  # noqa: E402
from app import paths as path_policy  # noqa: E402


class _Cfg:
    """The minimum `DesktopConfig` surface the policy reads."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.backend_addr = "127.0.0.1:1"  # unreachable on purpose: no probe in tests
        self.state_dir = repo_root / "Config"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Two granted directories, so nothing here depends on a real workspace."""
    workspace = tmp_path / "ZaytoonWorkspace"
    workspace.mkdir()
    other = tmp_path / "notes"
    other.mkdir()
    monkeypatch.setenv("ZAYTOON_OPEN_ROOTS", os.pathsep.join([str(workspace), str(other)]))
    return _Cfg(tmp_path), workspace, other


class TestContainment:
    def test_a_file_inside_a_granted_root_is_allowed(self, roots):
        cfg, workspace, _ = roots
        target = workspace / "report.md"
        target.write_text("hi", encoding="utf-8")
        allowed, reason = path_policy.classify(str(target), cfg)
        assert allowed, reason

    def test_a_path_outside_every_root_is_refused(self, roots):
        cfg, _, _ = roots
        # `sys.executable`, not `/etc/something`: the case is *containment*, so the
        # file has to exist, and on Windows and macOS `/etc/hostname` does not —
        # there it would be refused for the wrong reason and hide the real rule.
        allowed, reason = path_policy.classify(sys.executable, cfg)
        assert not allowed
        assert "outside every granted root" in reason, reason
        assert "ZAYTOON_OPEN_ROOTS" in reason, "the refusal must name the way to widen it"

    def test_dotdot_traversal_is_refused(self, roots):
        cfg, workspace, _ = roots
        sneaky = workspace / ".." / ".." / "etc" / "hostname"
        allowed, reason = path_policy.classify(str(sneaky), cfg)
        assert not allowed, reason

    def test_a_symlink_out_of_the_root_is_refused(self, roots):
        cfg, workspace, _ = roots
        link = workspace / "innocent.txt"
        try:
            link.symlink_to("/etc/hostname")
        except (OSError, NotImplementedError):  # pragma: no cover - Windows perms
            pytest.skip("this host/user cannot create symlinks")
        allowed, reason = path_policy.classify(str(link), cfg)
        assert not allowed, (
            "containment must be checked on the resolved target: a link inside the "
            "workspace pointing out of it is the oldest version of this bug"
        )

    def test_a_missing_path_is_refused(self, roots):
        cfg, workspace, _ = roots
        allowed, reason = path_policy.classify(str(workspace / "nope.txt"), cfg)
        assert not allowed and "does not exist" in reason

    def test_the_widening_env_actually_widens(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "granted"
        elsewhere.mkdir()
        (elsewhere / "a.txt").write_text("x", encoding="utf-8")
        monkeypatch.setenv("ZAYTOON_OPEN_ROOTS", str(elsewhere))
        assert path_policy.classify(str(elsewhere / "a.txt"), _Cfg(tmp_path))[0]
        monkeypatch.delenv("ZAYTOON_OPEN_ROOTS")
        # Without the grant, the same path is refused (the workspace fallbacks do
        # not exist in this tmp tree, so nothing here is accidentally allowed).
        assert not path_policy.classify(str(elsewhere / "a.txt"), _Cfg(tmp_path))[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX treats the execute bit as the risk")
class TestNothingRunnableIsOpened:
    def test_an_executable_in_the_workspace_is_still_refused(self, roots):
        cfg, workspace, _ = roots
        script = workspace / "payload.sh"
        script.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
        script.chmod(0o755)
        allowed, reason = path_policy.classify(str(script), cfg)
        assert not allowed
        assert "execute" in reason

    def test_a_desktop_entry_is_refused_even_without_the_bit(self, roots):
        """`.desktop` files are application launchers, not documents."""
        cfg, workspace, _ = roots
        entry = workspace / "evil.desktop"
        entry.write_text('[Desktop Entry]\nType=Application\nExec=touch /tmp/z-pwned\n', encoding="utf-8")
        entry.chmod(0o644)
        allowed, reason = path_policy.classify(str(entry), cfg)
        assert not allowed, reason
        assert "launch" in reason

    def test_a_plain_document_is_allowed(self, roots):
        cfg, workspace, _ = roots
        doc = workspace / "notes.txt"
        doc.write_text("x", encoding="utf-8")
        doc.chmod(0o644)
        assert path_policy.classify(str(doc), cfg)[0] is True


class TestWindowsExecutableTypes:
    def test_runnable_extensions_are_refused_by_suffix(self, roots):
        cfg, workspace, _ = roots
        target = workspace / "installer.exe"
        target.write_text("MZ", encoding="utf-8")
        class _WindowsOS:
            """`os` as the policy sees it on Windows: no execute bit, suffixes rule.

            It forwards everything else, because the policy also reads
            `os.environ` while resolving the granted roots.
            """

            name = "nt"

            def __getattr__(self, item):
                if item == "access":
                    return lambda *a, **k: pytest.fail(
                        "the POSIX execute-bit check ran on Windows"
                    )
                return getattr(os, item)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(path_policy, "os", _WindowsOS())
            allowed, reason = path_policy.classify(str(target), cfg)
            assert not allowed and "executable type" in reason
            # A document is still fine: the rule is about types, not about trust.
            doc = workspace / "report.docx"
            doc.write_text("x", encoding="utf-8")
            assert path_policy.classify(str(doc), cfg)[0]
        finally:
            monkeypatch.undo()

    @pytest.mark.parametrize(
        "name", ["run.exe", "a.bat", "b.cmd", "c.scr", "d.lnk", "e.ps1", "f.vbs", "g.jar", "h.hta"]
    )
    def test_the_suffix_list_covers_the_shell_handlers(self, name):
        assert Path(name).suffix.lower() in path_policy.EXECUTABLE_SUFFIXES


class TestUrlScheme:
    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("https://example.com/a?b=1", True),
            ("http://localhost:5173", True),
            ("HTTP://EXAMPLE.com", True),
            ("javascript:alert(1)", False),
            ("file:///etc/passwd", False),
            ("data:text/html,<script>alert(1)</script>", False),
            ("search-ms:query=zaytoon", False),
            ("mailto:someone@example.com", False),
            ("example.com/no-scheme", False),
            ("", False),
            ("   ", False),
        ],
    )
    def test_only_browser_schemes_pass(self, url, allowed):
        assert path_policy.classify_url(url)[0] is allowed


class TestBridgeConsultsThePolicy:
    def _bridge(self, cfg):
        return bridge_mod.NativeBridge(cfg, backend=None)

    def test_open_path_refuses_and_touches_nothing(self, roots, monkeypatch):
        cfg, workspace, _ = roots
        outside = sys.executable
        calls: list = []
        monkeypatch.setattr(
            bridge_mod.subprocess, "Popen", lambda *a, **k: calls.append(a)
        )
        monkeypatch.setattr(
            os, "startfile", lambda *a, **k: calls.append(a), raising=False
        )
        assert self._bridge(cfg).open_path(outside) is False
        assert calls == [], "a refused path must never reach the OS at all"

    def test_open_path_opens_the_resolved_target_once(self, roots, monkeypatch):
        cfg, workspace, _ = roots
        doc = workspace / "sub" / "a.txt"
        doc.parent.mkdir()
        doc.write_text("x", encoding="utf-8")
        link = workspace / "alias.txt"
        try:
            link.symlink_to(doc)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("this host/user cannot create symlinks")
        calls: list = []
        monkeypatch.setattr(
            bridge_mod.subprocess,
            "Popen",
            lambda argv, **k: calls.append(list(argv)) or types.SimpleNamespace(pid=0),
        )
        startfile: list = []
        monkeypatch.setattr(
            os, "startfile", lambda path, *a, **k: startfile.append(path), raising=False
        )
        assert self._bridge(cfg).open_path(str(link)) is True
        if os.name == "nt":
            # Windows does not go through `Popen` at all: `os.startfile` is the API,
            # which is why this case needs its own spy rather than an argv list.
            assert startfile == [str(doc.resolve())], (startfile, calls)
            assert calls == []
        else:
            # macOS resolves a "document" with `open`, Linux with `xdg-open` —
            # asserting the literal Linux argv everywhere is how this test was
            # written wrong and failed on the macos leg.
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            assert calls == [[opener, str(doc.resolve())]], calls
            assert startfile == []

    def test_reveal_in_os_refuses_outside_the_roots(self, roots, monkeypatch):
        cfg, _, _ = roots
        calls: list = []
        monkeypatch.setattr(
            bridge_mod.subprocess, "Popen", lambda *a, **k: calls.append(a)
        )
        assert self._bridge(cfg).reveal_in_os("/etc") is False
        assert calls == []

    def test_open_url_refuses_a_non_browser_scheme(self, roots, monkeypatch):
        cfg, _, _ = roots
        import webbrowser

        opened: list = []
        monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
        assert self._bridge(cfg).open_url("javascript:alert(1)") is False
        assert opened == []
        assert self._bridge(cfg).open_url("https://example.com") is True
        assert opened == ["https://example.com"]


def test_the_workspace_fallbacks_match_the_backend_default():
    """`app/paths.py` mirrors a rule it cannot import; the mirror is pinned.

    The desktop runs in its own interpreter and cannot read `backend/config.py`,
    so the workspace-directory names are duplicated here. A rename on one side has
    to break something, and this is that something — the same way
    `test_design_parity.py` pins the palette across the two clients.
    """
    backend_config = Path(__file__).resolve().parents[2] / "backend" / "config.py"
    source = backend_config.read_text(encoding="utf-8")
    assert 'DEFAULT_WORKSPACE_DIRNAME: str = "ZaytoonWorkspace"' in source
    assert "Workspaces" in source, (
        "the portable-install workspace directory moved in backend/config.py; "
        "update default_roots() in desktop/app/paths.py to match"
    )
    import app.config as desktop_config_mod

    listing = Path(path_policy.__file__).read_text(encoding="utf-8")
    for name in ("ZaytoonWorkspace", "Workspaces"):
        assert name in listing
