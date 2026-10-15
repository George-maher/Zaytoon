"""The unified tool registry and the tools added in Increment 2.

The registry replaced two independent tool stacks (``ToolExecutor`` and a
private copy inside ``ToolAgent``) that had already drifted apart. These tests
cover the contract every tool shares, plus the behaviour of the tools that did
not exist before: git, project creation, media, screenshots and ``open_url``.

Everything runs against a real temporary workspace — no mocking of the
filesystem — because the point of the containment layer is what happens on disk.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import sandbox  # noqa: E402
from tools.base import ToolResult  # noqa: E402
from tools.registry import build_default_registry  # noqa: E402
from tools.safety import PERMISSION_CONFIRM, PERMISSION_DANGEROUS, PERMISSION_SAFE  # noqa: E402


class _Approve:
    async def request(self, action, params=None, session_id=""):
        return True


class _Deny:
    async def request(self, action, params=None, session_id=""):
        return False


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Point every path tool at a temporary workspace."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(sandbox, "workspace_root", lambda: str(root))
    return root


@pytest.fixture
def gated(registry):
    """A registry wired to an always-approving channel."""
    return _Approve()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class TestRegistryContract:
    def test_every_tool_is_self_describing(self, registry):
        for tool in registry.tools():
            assert tool.name(), type(tool).__name__
            assert tool.description(), tool.name()
            schema = tool.schema()
            assert schema["type"] == "object", tool.name()
            assert isinstance(schema.get("properties"), dict), tool.name()
            assert tool.permission in (PERMISSION_SAFE, PERMISSION_CONFIRM, PERMISSION_DANGEROUS)

    def test_required_properties_actually_exist_in_properties(self, registry):
        """A schema requiring a field it does not declare is unusable."""
        for tool in registry.tools():
            schema = tool.schema()
            for required in schema.get("required", []):
                assert required in schema["properties"], (
                    f"{tool.name()} requires {required!r} but does not declare it"
                )

    def test_parameter_types_are_declared(self, registry):
        allowed = {"string", "integer", "boolean", "array", "object", "number"}
        for tool in registry.tools():
            for name, spec in tool.schema()["properties"].items():
                assert spec.get("type") in allowed, f"{tool.name()}.{name}"
                assert spec.get("description"), f"{tool.name()}.{name}"

    def test_aliases_point_at_registered_tools(self, registry):
        for tool in registry.tools():
            for alias in tool.aliases():
                assert registry.has(alias), f"{tool.name()} alias {alias!r} is dangling"
                assert registry.canonical(alias) == tool.name()

    def test_names_are_unique_and_stable(self, registry):
        names = registry.list()
        assert names == sorted(names)
        assert len(names) == len(set(names))

    def test_duplicate_registration_is_refused(self, registry):
        existing = registry.tools()[0]
        with pytest.raises(ValueError):
            registry.register(existing)

    def test_unknown_tool_is_a_clear_error(self, registry):
        result = asyncio.run(registry.execute("no_such_tool", {}))
        assert result["success"] is False
        assert "Unknown tool" in result["error"]

    def test_describe_is_json_serialisable(self, registry):
        import json

        json.dumps(registry.describe())
        json.dumps(registry.by_tier())

    def test_openai_specs_are_well_formed(self, registry):
        for spec in registry.openai_tools():
            assert spec["type"] == "function"
            assert spec["function"]["name"]
            assert spec["function"]["parameters"]["type"] == "object"


class TestGateOrdering:
    def test_validation_precedes_the_gate(self, registry):
        """A malformed request must not raise an approval prompt."""
        result = asyncio.run(registry.execute("file_read", {}))
        assert result["success"] is False
        assert not result.get("requires_confirmation")
        assert "required" in result["error"]

    def test_valid_risky_request_asks(self, registry):
        result = asyncio.run(registry.execute("file_write", {"path": "a.txt", "content": "x"}))
        assert result.get("requires_confirmation") is True
        assert result["permission"] == PERMISSION_CONFIRM

    def test_denial_stops_execution(self, registry, workspace, gated):
        target = workspace / "a.txt"
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "a.txt", "content": "x"},
                confirmation_manager=_Deny(),
            )
        )
        assert result["success"] is False
        assert result.get("cancelled") is True
        assert not target.exists()

    def test_approval_lets_it_through(self, registry, workspace, gated):
        asyncio.run(
            registry.execute(
                "file_write", {"path": "a.txt", "content": "hello"},
                confirmation_manager=gated,
            )
        )
        assert (workspace / "a.txt").read_text() == "hello"

    def test_no_channel_means_deny_not_run(self, registry, workspace):
        target = workspace / "b.txt"
        result = asyncio.run(
            registry.execute("file_write", {"path": "b.txt", "content": "x"})
        )
        assert result.get("requires_confirmation") is True
        assert not target.exists()

    def test_preapproved_skips_a_second_prompt(self, registry, workspace, gated):
        """The agent already asked; the registry must not ask again.

        Without `approval=True` this call is approved by the agent and then
        refused by the registry's own gate, which has no channel.
        """
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "c.txt", "content": "x"},
                confirmation_manager=None, approval=True,
            )
        )
        assert result["success"] is True
        assert (workspace / "c.txt").exists()

    def test_preapproved_false_still_blocks(self, registry, workspace):
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "d.txt", "content": "x"}, approval=False
            )
        )
        assert result.get("cancelled") is True
        assert not (workspace / "d.txt").exists()

    def test_sandbox_refusal_is_blocked_not_unconfirmed(self, registry):
        result = asyncio.run(
            registry.execute("file_read", {"path": "../../../Windows/system32/config"})
        )
        assert result.get("blocked") is True
        assert not result.get("requires_confirmation")


class TestResultShape:
    def test_success_carries_output_and_data(self, registry, workspace):
        (workspace / "f.txt").write_text("content", encoding="utf-8")
        result = asyncio.run(registry.execute("file_read", {"path": "f.txt"}))
        assert result["success"] is True
        assert "content" in result["output"]
        assert result["data"]["path"] == "f.txt"

    def test_failure_always_explains_itself(self, registry, workspace):
        result = asyncio.run(registry.execute("file_read", {"path": "nope.txt"}))
        assert result["success"] is False
        assert result["error"]

    def test_a_malformed_tool_result_is_not_trusted(self, registry):
        """A tool returning something other than a ToolResult is a bug in that
        tool, not a success. The registry must not pass it through."""
        from tools.base import BaseTool

        class _Bad(BaseTool):
            permission = "safe"

            def name(self):
                return "bad_tool"

            def description(self):
                return "Returns the wrong type."

            def schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return "just a string"

        reg = build_default_registry()
        reg.register(_Bad())
        out = asyncio.run(reg.execute("bad_tool", {}))
        assert out["success"] is False
        assert "Malformed" in out["error"]

    def test_a_success_with_no_output_still_explains_itself(self, registry):
        """A tool cannot report success without saying anything."""
        from tools.base import BaseTool, ToolResult

        class _Silent(BaseTool):
            permission = "safe"

            def name(self):
                return "silent_tool"

            def description(self):
                return "Says nothing."

            def schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=False, output="")

        reg = build_default_registry()
        reg.register(_Silent())
        out = asyncio.run(reg.execute("silent_tool", {}))
        assert out["success"] is False
        assert out["error"]


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------
class TestFilesystemTools:
    def test_read_relative_path(self, registry, workspace):
        (workspace / "a.txt").write_text("hi", encoding="utf-8")
        assert "hi" in asyncio.run(registry.execute("file_read", {"path": "a.txt"}))["output"]

    def test_read_nested(self, registry, workspace):
        (workspace / "x").mkdir()
        (workspace / "x" / "y.txt").write_text("deep", encoding="utf-8")
        assert "deep" in asyncio.run(registry.execute("file_read", {"path": "x/y.txt"}))["output"]

    def test_list_directory(self, registry, workspace):
        (workspace / "d").mkdir()
        (workspace / "d" / "one.txt").write_text("1", encoding="utf-8")
        out = asyncio.run(registry.execute("file_read", {"path": "d"}))["output"]
        assert "one.txt" in out

    def test_read_is_capped(self, registry, workspace):
        from tools.filesystem import MAX_READ_BYTES

        (workspace / "big.txt").write_text("x" * (MAX_READ_BYTES + 500), encoding="utf-8")
        out = asyncio.run(registry.execute("file_read", {"path": "big.txt"}))["output"]
        assert "truncated" in out

    def test_a_write_is_verified_by_reading_it_back(self, registry, workspace, gated):
        """The result says the effect was observed, not merely attempted.

        `write()` returning without raising means the call was accepted. The
        agent loop is told never to claim an action it has not observed, and
        this is where the observation happens — so the flag is set from the
        file on disk.
        """
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "verified.txt", "content": "landed"},
                confirmation_manager=gated,
            )
        )

        assert result["success"] is True
        assert result["verified"] is True
        assert (workspace / "verified.txt").read_text(encoding="utf-8") == "landed"

    def test_a_write_that_does_not_land_is_a_failure(self, registry, workspace, gated, monkeypatch):
        """The case the read-back exists for.

        Simulated by making the write a no-op, which is what a full volume, a
        path that turned out to be special, or a filesystem that filled up
        between the two syscalls all look like from here.
        """
        from tools import filesystem as filesystem_module

        monkeypatch.setattr(filesystem_module, "_write_file", lambda *a, **k: None)

        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "ghost.txt", "content": "never written"},
                confirmation_manager=gated,
            )
        )

        assert result["success"] is False
        # The message has to carry the truth either way: the bytes are not
        # there. Which of the two paths reports it depends on whether the file
        # exists at all when we look.
        assert "could not read it back" in result["error"]
        assert "verified" not in result or result.get("verified") is not True
        assert not (workspace / "ghost.txt").exists()

    def test_an_append_is_verified_against_its_tail(self, registry, workspace, gated):
        asyncio.run(
            registry.execute(
                "file_write", {"path": "tail.txt", "content": "first\n"},
                confirmation_manager=gated,
            )
        )
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "tail.txt", "content": "second\n", "append": True},
                confirmation_manager=gated,
            )
        )

        assert result["verified"] is True
        assert (workspace / "tail.txt").read_text(encoding="utf-8") == "first\nsecond\n"

    def test_a_tool_with_nothing_to_verify_does_not_claim_verification(self, registry, workspace):
        """`verified` is absent, not `false`, for a read-only tool.

        A `false` here would mean "we looked and it was not true"; for a tool
        that had no effect there is nothing to look at, and a client that
        renders "unverified" for `file_read` trains people to ignore the field
        on the results where it matters.
        """
        (workspace / "plain.txt").write_text("x", encoding="utf-8")
        result = asyncio.run(registry.execute("file_read", {"path": "plain.txt"}))

        assert result["success"] is True
        assert "verified" not in result

    def test_write_creates_parents(self, registry, workspace, gated):
        asyncio.run(
            registry.execute(
                "file_write", {"path": "deep/nested/new.txt", "content": "hi"},
                confirmation_manager=gated,
            )
        )
        assert (workspace / "deep" / "nested" / "new.txt").read_text() == "hi"

    def test_append(self, registry, workspace, gated):
        asyncio.run(
            registry.execute(
                "file_write", {"path": "log.txt", "content": "one\n"},
                confirmation_manager=gated,
            )
        )
        asyncio.run(
            registry.execute(
                "file_write", {"path": "log.txt", "content": "two\n", "append": True},
                confirmation_manager=gated,
            )
        )
        assert (workspace / "log.txt").read_text() == "one\ntwo\n"

    def test_write_rejects_non_string_content(self, registry, workspace, gated):
        result = asyncio.run(
            registry.execute(
                "file_write", {"path": "a.txt", "content": {"not": "a string"}},
                confirmation_manager=gated,
            )
        )
        assert result["success"] is False
        assert "must be a string" in result["error"]

    def test_create_folder(self, registry, workspace, gated):
        asyncio.run(
            registry.execute("create_folder", {"path": "AI Projects"}, confirmation_manager=gated)
        )
        assert (workspace / "AI Projects").is_dir()

    def test_create_folder_is_idempotent(self, registry, workspace, gated):
        for _ in range(2):
            result = asyncio.run(
                registry.execute("create_folder", {"path": "twice"}, confirmation_manager=gated)
            )
        assert result["success"] is True

    def test_delete_file(self, registry, workspace, gated):
        (workspace / "gone.txt").write_text("x", encoding="utf-8")
        asyncio.run(
            registry.execute("file_delete", {"path": "gone.txt"}, confirmation_manager=gated)
        )
        assert not (workspace / "gone.txt").exists()

    def test_delete_non_empty_dir_requires_recursive(self, registry, workspace, gated):
        (workspace / "dir").mkdir()
        (workspace / "dir" / "f.txt").write_text("x", encoding="utf-8")
        result = asyncio.run(
            registry.execute("file_delete", {"path": "dir"}, confirmation_manager=gated)
        )
        assert result["success"] is False
        assert "recursive" in result["error"]
        assert (workspace / "dir").exists()

    def test_delete_dir_recursive(self, registry, workspace, gated):
        (workspace / "dir").mkdir()
        (workspace / "dir" / "f.txt").write_text("x", encoding="utf-8")
        asyncio.run(
            registry.execute(
                "file_delete", {"path": "dir", "recursive": True},
                confirmation_manager=gated,
            )
        )
        assert not (workspace / "dir").exists()

    def test_move(self, registry, workspace, gated):
        (workspace / "src.txt").write_text("payload", encoding="utf-8")
        asyncio.run(
            registry.execute(
                "file_move", {"source": "src.txt", "destination": "dst.txt"},
                confirmation_manager=gated,
            )
        )
        assert not (workspace / "src.txt").exists()
        assert (workspace / "dst.txt").read_text() == "payload"

    def test_copy_keeps_the_original(self, registry, workspace, gated):
        (workspace / "a.txt").write_text("x", encoding="utf-8")
        asyncio.run(
            registry.execute(
                "file_move", {"source": "a.txt", "destination": "b.txt", "copy": True},
                confirmation_manager=gated,
            )
        )
        assert (workspace / "a.txt").exists() and (workspace / "b.txt").exists()

    def test_search_contents(self, registry, workspace):
        (workspace / "code.py").write_text("def needle():\n    pass\n", encoding="utf-8")
        out = asyncio.run(registry.execute("search_files", {"query": "needle"}))["output"]
        assert "code.py:1" in out

    def test_search_regex(self, registry, workspace):
        (workspace / "c.py").write_text("value = 42\n", encoding="utf-8")
        out = asyncio.run(
            registry.execute("search_files", {"query": r"value = \d+", "regex": True})
        )["output"]
        assert "c.py" in out

    def test_search_rejects_invalid_regex(self, registry, workspace):
        result = asyncio.run(
            registry.execute("search_files", {"query": "[unclosed", "regex": True})
        )
        assert result["success"] is False

    def test_search_prunes_noise_directories(self, registry, workspace):
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "junk.js").write_text("needle", encoding="utf-8")
        (workspace / "real.js").write_text("needle", encoding="utf-8")
        out = asyncio.run(registry.execute("search_files", {"query": "needle"}))["output"]
        assert "real.js" in out
        assert "node_modules" not in out

    def test_search_results_are_capped(self, registry, workspace):
        (workspace / "many.txt").write_text("\n".join(["hit"] * 500), encoding="utf-8")
        result = asyncio.run(
            registry.execute("search_files", {"query": "hit", "max_results": 10})
        )
        assert result["data"]["count"] == 10
        assert result["data"]["truncated"] is True

    def test_every_escape_attempt_is_blocked(self, registry, workspace):
        """No escape attempt may end up reading something outside the workspace.

        The assertion is about the outcome, not the mechanism.
        ``C:/Windows/…`` is an absolute path in Windows and a perfectly ordinary
        *relative* name on POSIX — a directory literally called ``C:`` — so on
        Linux the honest question is not "was it refused?" but "did it stay
        inside the root?". Asserting `blocked` everywhere tested the shape of
        the string, and reported a containment bug on a machine where the
        containment was correct.
        """
        for path in (
            "../outside.txt", "../../etc/passwd", "a/../../b",
            "C:/Windows/System32/drivers/etc/hosts", "~/secret",
        ):
            result = asyncio.run(registry.execute("file_read", {"path": path}))
            if os.name == "nt":
                # Every one of these is an escape on Windows, including the
                # drive-absolute one, and `file_read` is tier `safe` so the only
                # possible refusal is the sandbox's.
                assert result.get("blocked") is True, path
                continue
            if result.get("blocked"):
                continue
            resolved = sandbox.resolve_workspace(path, str(workspace))
            assert resolved, f"{path} was neither refused nor resolved"
            assert os.path.commonpath([resolved, str(workspace)]) == str(workspace), path


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
class TestSystemTools:
    def test_get_time(self, registry):
        out = asyncio.run(registry.execute("get_time", {}))["output"]
        assert ":" in out

    def test_system_info_is_structured(self, registry):
        result = asyncio.run(registry.execute("system_info", {}))
        assert result["success"] is True
        for key in ("os", "machine", "python", "cpu_count"):
            assert key in result["data"], key

    def test_calculator_arithmetic(self, registry):
        assert "390" in asyncio.run(
            registry.execute("calculator", {"expression": "15 * 23 + 45"})
        )["output"]

    def test_calculator_refuses_code(self, registry):
        for evil in ("__import__('os').system('ls')", "open('/etc/passwd')", "[1,2,3]"):
            result = asyncio.run(registry.execute("calculator", {"expression": evil}))
            assert result["success"] is False, evil

    def test_calculator_division_by_zero_is_reported(self, registry):
        result = asyncio.run(registry.execute("calculator", {"expression": "1/0"}))
        assert result["success"] is False

    def test_screenshot_requires_png(self, registry):
        result = asyncio.run(registry.execute("screenshot", {"filename": "evil.exe"}))
        assert result["success"] is False
        assert ".png" in result["error"]


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
class TestWebTools:
    def test_open_url_accepts_https(self, registry):
        from tools.web import OpenUrl

        # Canonical, not echoed: `_check` returns netguard's rebuilt URL, which is
        # the whole point of delegating — the string the browser receives is the
        # one whose host and port were actually judged.
        assert OpenUrl._check("https://example.com") == "https://example.com/"

    def test_open_url_adds_a_missing_scheme(self, registry):
        from tools.web import OpenUrl

        assert OpenUrl._check("example.com") == "https://example.com/"
        # A bare host with a port is a *port*, not a scheme. `re.match`-style
        # tests read it as `pypi.org:` and produce a nonsense host.
        assert OpenUrl._check("pypi.org:443/project") == "https://pypi.org/project"

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://[fe80::1]/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
        ],
    )
    def test_open_url_refuses_the_addresses_fetch_url_refuses(self, registry, url):
        """The point of delegating: one address policy for both tools.

        A page the model read can instruct it to "open this for me", so an
        `open_url` that accepted what `fetch_url` refuses is a hole even though
        the browser, not the service, makes the request.
        """
        result = asyncio.run(registry.execute("open_url", {"url": url}))
        assert result.get("blocked") is True, url
        assert "not openable" in result["error"], result

    def test_open_url_obeys_its_own_private_network_knob(self, registry, monkeypatch):
        """Loopback is allowed by default and refused when told not to be.

        The default differs from `fetch_url`'s on purpose: this hands a URL to
        the user's own browser ("open my router page"), it does not make the
        service read something. See `OPEN_URL_ALLOW_PRIVATE` in config.py.
        """
        from config import config
        from tools.web import OpenUrl

        assert OpenUrl._check("localhost:5173") == "https://localhost:5173/"
        monkeypatch.setattr(config, "OPEN_URL_ALLOW_PRIVATE", False)
        result = asyncio.run(registry.execute("open_url", {"url": "http://localhost:5173"}))
        assert result.get("blocked") is True
        assert "this machine" in result["error"]

    def test_open_url_refuses_credentials_in_the_url(self, registry):
        result = asyncio.run(registry.execute("open_url", {"url": "http://admin@evil.test/"}))
        assert result.get("blocked") is True
        assert "credentials" in result["error"]

    @pytest.mark.parametrize(
        "url",
        [
            "file:///C:/Windows/System32/config",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "ftp://example.com",
        ],
    )
    def test_open_url_refuses_non_http_schemes(self, registry, url):
        """A desktop assistant opening a `file://` or `javascript:` URL is a
        local-read and script-execution primitive respectively."""
        result = asyncio.run(registry.execute("open_url", {"url": url}))
        assert result.get("blocked") is True, url

    def test_open_url_rejects_a_bare_hostname_with_no_path(self, registry):
        result = asyncio.run(registry.execute("open_url", {"url": "https://"}))
        assert result["success"] is False

    def test_search_requires_a_query(self, registry):
        result = asyncio.run(registry.execute("search_web", {"query": ""}))
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Coding
# ---------------------------------------------------------------------------
class TestCreateProject:
    def _create(self, registry, workspace, gated, **kwargs):
        return asyncio.run(
            registry.execute("create_project", kwargs, confirmation_manager=gated)
        )

    def test_creates_a_react_project(self, registry, workspace, gated):
        result = self._create(registry, workspace, gated, name="AI Dashboard", template="react")
        assert result["success"] is True
        project = workspace / "ai-dashboard"
        assert (project / "package.json").is_file()
        assert (project / "index.html").is_file()
        assert (project / "src" / "App.jsx").is_file()
        assert (project / "src" / "main.jsx").is_file()

    def test_react_manifest_declares_its_dependencies(self, registry, workspace, gated):
        import json

        self._create(registry, workspace, gated, name="proj", template="react")
        manifest = json.loads((workspace / "proj" / "package.json").read_text())
        assert "react" in manifest["dependencies"]
        assert "vite" in manifest["devDependencies"]
        assert "dev" in manifest["scripts"]

    def test_creates_a_python_project(self, registry, workspace, gated):
        self._create(registry, workspace, gated, name="My Tool", template="python")
        project = workspace / "my-tool"
        assert (project / "my_tool.py").is_file()
        assert (project / "pyproject.toml").is_file()

    def test_creates_an_html_project(self, registry, workspace, gated):
        self._create(registry, workspace, gated, name="Site", template="html")
        project = workspace / "site"
        assert (project / "index.html").is_file()
        assert (project / "style.css").is_file()

    def test_node_project_runs(self, registry, workspace, gated):
        self._create(registry, workspace, gated, name="tool", template="node")
        import subprocess

        proc = subprocess.run(
            ["node", "index.js"], cwd=workspace / "tool",
            capture_output=True, text=True, timeout=30, shell=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "tool running" in proc.stdout

    def test_writes_a_gitignore(self, registry, workspace, gated):
        self._create(registry, workspace, gated, name="p", template="react")
        assert (workspace / "p" / ".gitignore").is_file()

    def test_refuses_to_overwrite(self, registry, workspace, gated):
        self._create(registry, workspace, gated, name="dup")
        second = self._create(registry, workspace, gated, name="dup")
        assert second["success"] is False
        assert "already exists" in second["error"]

    @pytest.mark.parametrize(
        "name", ["../escape", "with/slash", "..", "a" * 100, "con", ""]
    )
    def test_rejects_dangerous_names(self, registry, workspace, gated, name):
        result = self._create(registry, workspace, gated, name=name)
        assert result["success"] is False, name

    def test_rejects_an_unknown_template(self, registry, workspace, gated):
        result = self._create(registry, workspace, gated, name="x", template="cobol")
        assert result["success"] is False

    def test_requires_confirmation(self, registry, workspace):
        result = asyncio.run(registry.execute("create_project", {"name": "x"}))
        assert result.get("requires_confirmation") is True
        assert not (workspace / "x").exists()


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
class TestGitTools:
    @pytest.fixture
    def repo(self, workspace, tmp_path):
        """A real repository in the workspace, with one commit."""
        import subprocess

        root = workspace / "repo"
        root.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "Test"],
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=env)
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=root, check=True, capture_output=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=root, check=True, capture_output=True, env=env,
        )
        return root

    def _run(self, registry, name, params, gated=None, **kw):
        return asyncio.run(
            registry.execute(name, {**params, "path": "repo"}, confirmation_manager=gated, **kw)
        )

    def test_status_is_clean(self, registry, repo):
        result = self._run(registry, "git_status", {})
        assert result["success"] is True
        assert "main" in result["output"]

    def test_status_shows_modifications(self, registry, repo):
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        assert "M a.txt" in self._run(registry, "git_status", {})["output"]

    def test_log(self, registry, repo):
        result = self._run(registry, "git_log", {"limit": 5})
        assert "initial" in result["output"]

    def test_diff(self, registry, repo):
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        result = self._run(registry, "git_diff", {})
        assert "changed" in result["output"]

    def test_branch_list(self, registry, repo):
        assert "main" in self._run(registry, "git_branch_list", {})["output"]

    def test_add_and_commit(self, registry, repo, gated):
        (repo / "b.txt").write_text("two\n", encoding="utf-8")
        assert self._run(registry, "git_add", {"files": ["b.txt"]}, gated)["success"]
        result = self._run(registry, "git_commit", {"message": "add b"}, gated)
        assert result["success"] is True
        assert "add b" in self._run(registry, "git_log", {"limit": 1})["output"]

    def test_read_only_git_does_not_prompt(self, registry, repo):
        for name in ("git_status", "git_log", "git_diff", "git_branch_list"):
            result = self._run(registry, name, {})
            assert not result.get("requires_confirmation"), name

    def test_mutating_git_always_prompts(self, registry, repo):
        for name, params in (
            ("git_add", {"files": ["a.txt"]}),
            ("git_commit", {"message": "x"}),
            ("git_checkout", {"ref": "other"}),
            ("git_push", {"remote": "origin"}),
        ):
            result = self._run(registry, name, params)
            assert result.get("requires_confirmation") is True, name

    def test_commit_needs_a_message(self, registry, repo, gated):
        result = self._run(registry, "git_commit", {"message": ""}, gated)
        assert result["success"] is False
        assert "message" in result["error"]

    @pytest.mark.parametrize(
        "ref", ["main; rm -rf /", "--upload-pack=evil", "a\nb", "-x", "`id`", "$(id)"]
    )
    def test_ref_argument_injection_is_refused(self, registry, repo, ref):
        """Refs go into argv, but a value with shell or option syntax is still
        refused — the tool's output goes back into a model context."""
        result = asyncio.run(
            registry.execute("git_checkout", {"ref": ref}, approval=True)
        )
        assert result["success"] is False, ref

    def test_log_limit_is_bounded(self, registry, repo):
        assert asyncio.run(
            registry.execute("git_log", {"limit": 9999}, approval=True)
        )["success"] is False

    def test_operations_outside_a_repo_fail_cleanly(self, registry, workspace, gated):
        (workspace / "not-a-repo").mkdir()
        result = self._run(registry, "git_status", {}, gated)
        assert result["success"] is False
        assert result["error"]


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class TestMediaTools:
    def test_rejects_an_unknown_action(self, registry):
        result = asyncio.run(registry.execute("play_media", {"action": "self_destruct"}))
        assert result["success"] is False

    def test_known_actions(self, registry):
        from tools.media import _KEYS

        for action in _KEYS:
            result = asyncio.run(registry.execute("play_media", {"action": action}, approval=True))
            # Either it worked, or it reported the platform is unsupported.
            assert "only implemented on Windows" in result.get("error", "") or result["success"], action

    def test_requires_confirmation(self, registry):
        result = asyncio.run(registry.execute("play_media", {"action": "play"}))
        assert result.get("requires_confirmation") is True

    def test_bounds_the_repeat_count(self, registry):
        result = asyncio.run(
            registry.execute("play_media", {"action": "play", "times": 9999}, approval=True)
        )
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------
class TestExecuteMany:
    def test_runs_in_order(self, registry, workspace, gated):
        results = asyncio.run(
            registry.execute_many(
                [
                    {"name": "create_folder", "params": {"path": "step1"}},
                    {"name": "file_write", "params": {"path": "step1/out.txt", "content": "x"}},
                ],
                confirmation_manager=gated,
            )
        )
        assert [r["success"] for r in results] == [True, True]
        assert (workspace / "step1" / "out.txt").exists()

    def test_accepts_the_action_params_spelling(self, registry, workspace, gated):
        results = asyncio.run(
            registry.execute_many(
                [{"action": "get_time", "params": {}}], confirmation_manager=gated
            )
        )
        assert results[0]["success"] is True

    def test_empty_batch(self, registry):
        assert asyncio.run(registry.execute_many([])) == []


def test_no_tool_can_write_the_provider_registry(registry):
    """Provider configuration is reachable only through the settings API.

    An endpoint is an SSRF decision: whoever can register one can point the
    backend's HTTP client at a host of their choosing. That authority belongs
    to the human, at the settings screen — never to a model selecting a tool.
    There is deliberately no provider-administration tool; this test is the
    tripwire if one is ever added.
    """
    import re

    offending = [
        tool.name for tool in registry.tools()
        if re.search(r"provider", f"{tool.name} {tool.description}", re.I)
    ]
    assert offending == [], (
        "these tools would let a model redirect the backend's traffic: "
        f"{offending}"
    )
