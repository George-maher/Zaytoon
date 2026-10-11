"""The shipped security skills are passive because nothing else is possible.

"Passive only, regardless of authorization" was a product decision
(2026-10-11), and a decision written in a README is a wish. So the guarantee is
checked structurally here: the skill modules may not *import* anything that could
reach the network or run a program, may not open a file for writing, and may not
call the dozen `os`/`shutil` functions that change state. That is what makes
these tools auditable by reading 30 lines of imports instead of tracing behaviour.

Plus the two properties that are easy to lose by accident: they install disabled
(a skill injects instructions into the model's context, which is a change in
behaviour, not a package), and they are floored at `confirm` like every other
skill's tools — being shipped by us is not an exemption from the permission floor.
"""

import ast
import asyncio
import json
import os
import pathlib
import shutil
from typing import Iterable, List

import pytest

from core.permissions import gaps_for
from core.skills import load_skill_tools, set_enabled, sync_to_db
from core.skills.manifest import MAX_INSTRUCTIONS_CHARS, load

REPO_SKILLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "skills",
)

SHIPPED = sorted(
    name for name in os.listdir(REPO_SKILLS)
    if os.path.isfile(os.path.join(REPO_SKILLS, name, "skill.json"))
) if os.path.isdir(REPO_SKILLS) else []

#: The entire import surface a shipped security tool may need. Anything not here
#: has to be argued about and added, which is the point of the list.
ALLOWED_IMPORTS = {
    "__future__", "json", "hashlib", "os", "re", "typing", "tools.base",
    "tools.sandbox", "tools.safety", "tools.registry",
}

#: Attribute calls that would end the read-only property.
#: `os.walk` and `os.stat` are reads and stay off this list on purpose; a listing
#: that bans all of `os` would only teach the next author to reach for a
#: subprocess instead.
FORBIDDEN_ATTRS = {
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.rmdir", "os.mkdir",
    "os.makedirs", "os.chmod", "os.chown", "os.symlink", "os.link", "os.system",
    "os.popen", "os.kill", "os.fork", "os.execv", "os.execve", "os.spawnl",
    "os.startfile", "os.putenv", "os.truncate", "os.ftruncate",
    "shutil.rmtree", "shutil.copy", "shutil.copyfile", "shutil.copytree",
    "shutil.move", "shutil.chown", "shutil.rmtree",
}

#: Names that must not be called at all, however they were obtained.
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input"}


def run(coro):
    return asyncio.run(coro)


def _sources(directory: str) -> Iterable[tuple]:
    tools_dir = os.path.join(directory, "tools")
    for entry in sorted(os.listdir(tools_dir)):
        if entry.endswith(".py") and not entry.startswith("_"):
            path = os.path.join(tools_dir, entry)
            yield entry, path, ast.parse(open(path, encoding="utf-8").read())


def _module_of(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class TestShipLayout:
    def test_there_are_shipped_skills_to_test(self):
        """A glob that matches nothing would make every test below pass vacuously."""
        assert len(SHIPPED) >= 3, SHIPPED

    def test_every_manifest_validates_and_installs_disabled(self):
        for name in SHIPPED:
            manifest = load(os.path.join(REPO_SKILLS, name))
            assert manifest.name == name
            assert manifest.enabled is False, (
                f"{name}: a shipped skill that turns itself on changes what the "
                "model is told, without anyone agreeing to it"
            )

    def test_the_declared_policy_is_exactly_read(self):
        for name in SHIPPED:
            manifest = load(os.path.join(REPO_SKILLS, name))
            assert set(manifest.permissions) == {"filesystem.read"}, (
                f"{name}: a passive security skill asks for reads and nothing else"
            )

    def test_the_instructions_state_the_boundary_and_fit_the_cap(self):
        for name in SHIPPED:
            directory = os.path.join(REPO_SKILLS, name)
            text = open(
                os.path.join(directory, "instructions.md"), encoding="utf-8"
            ).read()
            assert len(text) <= MAX_INSTRUCTIONS_CHARS
            for phrase in ("Never probe, scan", "Never change anything", "Unverified"):
                pass  # phrase checks are done per-file below, so a rename is visible
            assert "Never probe, scan" in text, f"{name}: the refusal is the point"
            assert "Never change anything" in text or "Never exfiltrate" in text
            assert os.path.isfile(os.path.join(directory, "README.md"))


class TestPassiveByConstruction:
    @pytest.mark.parametrize("skill", SHIPPED)
    def test_no_import_outside_the_read_only_set(self, skill):
        for filename, _, tree in _sources(os.path.join(REPO_SKILLS, skill)):
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name in ALLOWED_IMPORTS, (
                            f"{skill}/{filename}:{node.lineno} imports {alias.name!r}; "
                            "a shipped security skill may not gain a dependency "
                            "quietly (subprocess, socket, requests, ssl, …)"
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert node.module in ALLOWED_IMPORTS or root in ALLOWED_IMPORTS, (
                        f"{skill}/{filename}:{node.lineno} imports from "
                        f"{node.module!r}, which is not on the allowed list"
                    )

    @pytest.mark.parametrize("skill", SHIPPED)
    def test_no_call_that_could_change_the_machine(self, skill):
        for filename, _, tree in _sources(os.path.join(REPO_SKILLS, skill)):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in FORBIDDEN_CALLS, (
                        f"{skill}/{filename}:{node.lineno} calls {node.func.id}()"
                    )
                dotted = _module_of(node.func) if isinstance(
                    node.func, (ast.Attribute, ast.Name)
                ) else ""
                if dotted:
                    assert dotted not in FORBIDDEN_ATTRS, (
                        f"{skill}/{filename}:{node.lineno} calls {dotted}() — that is "
                        "not a read"
                    )
                    head = dotted.rsplit(".", 1)[0]
                    if head in {"subprocess", "socket", "http", "urllib", "requests",
                                "httpx", "webbrowser", "pty", "ctypes", "ssl"}:
                        raise AssertionError(
                            f"{skill}/{filename}:{node.lineno} uses {dotted}()"
                        )

    @pytest.mark.parametrize("skill", SHIPPED)
    def test_no_file_is_opened_for_writing(self, skill):
        for filename, _, tree in _sources(os.path.join(REPO_SKILLS, skill)):
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _module_of(node.func) == "open":
                    mode = ""
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        mode = str(node.args[1].value)
                    for keyword in node.keywords:
                        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                            mode = str(keyword.value.value)
                    assert not any(ch in mode for ch in "wax+"), (
                        f"{skill}/{filename}:{node.lineno} opens with mode "
                        f"{mode!r}: an audit tool that can write is not an audit tool"
                    )

    @pytest.mark.parametrize("skill", SHIPPED)
    def test_declared_permissions_cover_what_the_tools_need(self, skill):
        """The P6 gap check, run against the shipped set rather than a fixture.

        `filesystem.read` is what these manifests declare, so a tool whose name
        implies writes — or that grows one later — fails here before it ships.
        """
        from core.skills.tool_loader import _declared_permissions
        from tools.base import BaseTool

        manifest = None
        for tool_name, declared in self._tools(skill):
            manifest = load(os.path.join(REPO_SKILLS, skill))
            gaps = gaps_for(tool_name, declared, manifest.permissions)
            assert gaps == frozenset(), f"{skill}: {tool_name} needs {sorted(gaps)}"

    @staticmethod
    def _tools(skill: str):
        """Import the shipped module and read its tools' names + declarations."""
        import importlib.util
        import sys

        tools_dir = os.path.join(REPO_SKILLS, skill, "tools")
        for entry in sorted(os.listdir(tools_dir)):
            if not entry.endswith(".py") or entry.startswith("_"):
                continue
            path = os.path.join(tools_dir, entry)
            name = f"shipped_probe_{skill}_{entry[:-3]}"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            for tool in module.build_tools():
                yield tool.name(), tool.permissions()


class TestLoaderIntegration:
    def test_they_load_cleanly_into_a_real_registry(self, temp_skills_dir, temp_core_db):
        """End to end: copy → sync → enable → load, with nothing to complain about."""
        for name in SHIPPED:
            shutil.copytree(
                os.path.join(REPO_SKILLS, name), os.path.join(temp_skills_dir, name)
            )
        run(sync_to_db(temp_skills_dir))
        for name in SHIPPED:
            assert run(set_enabled(name, True)) is True

        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        report = run(load_skill_tools(registry, root=temp_skills_dir))

        assert report["errors"] == [], report["errors"]
        assert report["policy"] == [], "a shipped skill must not out-ask its policy"
        loaded = {name.split(".", 1)[1] for name in report["tools"]}
        assert loaded == {
            "security_scan_logs", "security_review_dependencies",
            "security_hash_files", "security_inspect_paths",
        }, loaded

        # Shipped by us, floored like everyone else: the `confirm` floor has no
        # exception list, because an exception list is where it would leak.
        for tool_name in loaded:
            assert registry.effective_tier(tool_name) == "confirm", tool_name
            assert registry.floor_for(tool_name) == "confirm"


def _workspace(temp_workspace):
    """The temp workspace the shared fixture points `config.workspace_path` at.

    Not a local monkeypatch of `workspace_path`: it is a read-only property, and
    the repo's own fixture redirects it through `WORKSPACE_ROOT` so the tests
    exercise the real containment resolution instead of a stubbed helper.
    """
    return pathlib.Path(temp_workspace)


class TestTheyActuallyWork:
    def test_a_key_in_a_log_is_found_and_masked(self, tmp_path, temp_workspace):
        root = _workspace(temp_workspace)
        (root / "app.log").write_text(
            "2026-10-11 05:00:1 auth: Failed password for invalid user admin from 10.0.0.9\n"
            "2026-10-11 05:00:4 sdk: using key AKIAIOSFODNN7EXAMPLE for upload\n"
            "2026-10-11 05:00:9 GET /health 200\n"
        )
        result = run(self._tool("log-review", "security_scan_logs").execute())
        assert result.success
        text = result.output
        assert "aws-access-key=1" in text and "auth-failure=1" in text
        assert "AKIAIOSFODNN7EXAMPLE" not in text, "the finding pasted the secret"
        assert "AKIA…MPLE" in text
        assert "app.log:2" in text

    def test_npm_hooks_and_pip_holes_are_reported_offline(self, tmp_path, temp_workspace):
        root = _workspace(temp_workspace)
        (root / "package.json").write_text(json.dumps({
            "name": "app", "scripts": {"postinstall": "node ./install.js"},
            "dependencies": {"left-pad": "latest", "evil": "git+https://x.test/e.git"},
        }))
        (root / "requirements.txt").write_text(
            "requests==2.31.0\nflask\nhttpx @ git+https://x.test/h.git\n"
        )
        result = run(self._tool("dependency-review", "security_review_dependencies").execute())
        assert result.success
        text = result.output
        for label in ("install-script-hook", "git-source", "unpinned-version"):
            assert label in text, label
        assert "no risky shapes found" not in text

    def test_a_clean_project_says_so_without_inventing_findings(self, tmp_path, temp_workspace):
        root = _workspace(temp_workspace)
        (root / "requirements.txt").write_text("requests==2.31.0\n")
        result = run(self._tool("dependency-review", "security_review_dependencies").execute())
        assert "0 finding(s)" in result.output
        assert "advisory database" in result.output  # states what it could not do

    def test_nothing_outside_the_workspace_is_readable(self, tmp_path, temp_workspace):
        root = _workspace(temp_workspace)
        outside = tmp_path / "secret.env"
        outside.write_text("TOKEN=abc123\n")
        hashes = self._tool("file-audit", "security_hash_files")
        result = run(hashes.execute(paths=[str(outside)]))
        assert "refused as outside the workspace" in result.output
        assert outside.read_text().strip() not in result.output

        inspect = self._tool("file-audit", "security_inspect_paths")
        second = run(inspect.execute(paths=[str(outside)]))
        assert "REFUSED" in second.output

    def test_hashes_match_a_known_digest_and_modes_are_flagged(self, tmp_path, temp_workspace):
        import hashlib

        root = _workspace(temp_workspace)
        payload = b"the exact bytes\n"
        target = root / "payload.bin"
        target.write_bytes(payload)
        os.chmod(target, 0o666)

        digests = hashlib.sha256(payload).hexdigest()
        result = run(self._tool("file-audit", "security_hash_files").execute(
            paths=["payload.bin"], expected=digests
        ))
        assert "MATCH" in result.output and digests in result.output

        wrong = run(self._tool("file-audit", "security_hash_files").execute(
            paths=["payload.bin"], expected="0" * 64
        ))
        assert "MISMATCH" in wrong.output

        inspected = run(self._tool("file-audit", "security_inspect_paths").execute(
            paths=["payload.bin"]
        ))
        assert "world-writable" in inspected.output
        assert "0o666" in inspected.output

    def test_a_certificate_is_reported_as_unread_rather_than_clean(self, tmp_path, temp_workspace):
        root = _workspace(temp_workspace)
        (root / "server.crt").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n")
        result = run(self._tool("file-audit", "security_inspect_paths").execute(
            paths=["server.crt"]
        ))
        assert "PEM certificate" in result.output
        assert "not decoded" in result.output, (
            "skipping a check and calling the result clean is the failure this "
            "skill exists to avoid"
        )

    @staticmethod
    def _tool(skill: str, tool_name: str):
        import importlib.util
        import sys

        path = next(
            os.path.join(REPO_SKILLS, skill, "tools", entry)
            for entry in os.listdir(os.path.join(REPO_SKILLS, skill, "tools"))
            if entry.endswith(".py")
        )
        name = f"shipped_func_{skill}_{tool_name}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return next(t for t in module.build_tools() if t.name() == tool_name)
