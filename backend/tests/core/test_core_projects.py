"""Project detection, registry, and the context block."""

import json
import os

import pytest

from core.projects import (
    ProjectRejected,
    build_context,
    context_from_path,
    detect,
    get,
    get_by_path,
    is_within,
    list_projects,
    project_id_for,
    recent,
    register,
    remove,
    set_pinned,
    update_commands,
    validate_path,
    workspace_summary,
)


def make_node_project(root, scripts=None, deps=None, lockfile=None):
    manifest = {"name": "demo", "scripts": scripts or {}, "dependencies": deps or {}}
    with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    if lockfile:
        open(os.path.join(root, lockfile), "w").close()
    return root


def make_python_project(root, test_dir=False):
    with open(os.path.join(root, "requirements.txt"), "w", encoding="utf-8") as handle:
        handle.write("fastapi\n")
    if test_dir:
        os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    return root


class TestDetectNode:
    def test_detects_node(self, temp_workspace):
        make_node_project(temp_workspace)
        profile = detect(temp_workspace)
        assert profile.kind == "node"
        assert profile.language == "node"

    def test_reads_scripts(self, temp_workspace):
        make_node_project(temp_workspace, scripts={
            "dev": "vite", "test": "vitest", "build": "vite build",
        })
        profile = detect(temp_workspace)
        assert profile.run_command == "npm run dev"
        # `npm run test`, not the shorter `npm test`: the bare form is not
        # uniform across package managers and `bun test` runs bun's own runner.
        assert profile.test_command == "npm run test"
        assert profile.build_command == "npm run build"

    def test_a_missing_script_is_none_not_a_guess(self, temp_workspace):
        make_node_project(temp_workspace, scripts={"dev": "vite"})
        profile = detect(temp_workspace)
        assert profile.test_command is None
        assert any("No dev/start script" in note for note in profile.notes) is False

    @pytest.mark.parametrize("lockfile,expected", [
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
    ])
    def test_lockfile_decides_the_package_manager(self, temp_workspace, lockfile, expected):
        # A separate directory per lockfile: one shared directory would keep the
        # first lockfile and the detection would (correctly) keep preferring it.
        root = os.path.join(temp_workspace, lockfile.split(".")[0])
        os.makedirs(root)
        make_node_project(root, lockfile=lockfile)
        assert detect(root).package_manager == expected

    def test_pnpm_wins_when_several_lockfiles_remain(self, temp_workspace):
        """A repository with a stale pnpm lock is still a pnpm repository."""
        make_node_project(temp_workspace, lockfile="pnpm-lock.yaml")
        make_node_project(temp_workspace, lockfile="yarn.lock")
        assert detect(temp_workspace).package_manager == "pnpm"

    def test_the_package_manager_reaches_the_script_command(self, temp_workspace):
        make_node_project(
            temp_workspace,
            scripts={"test": "vitest"},
            lockfile="pnpm-lock.yaml",
        )
        assert detect(temp_workspace).test_command == "pnpm test"

    def test_detects_the_framework_from_dependencies(self, temp_workspace):
        make_node_project(temp_workspace, deps={"next": "15.0.0"})
        assert detect(temp_workspace).framework == "next"

        make_node_project(temp_workspace, deps={"vite": "6", "react": "19"})
        assert detect(temp_workspace).framework == "react-vite"

    def test_a_huge_manifest_is_not_parsed(self, temp_workspace):
        """A monorepo manifest is megabytes and detection is on the request path."""
        path = os.path.join(temp_workspace, "package.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"name": "big", "scripts": {"dev": "x"}, "pad": "')
            handle.write("A" * 300_000)
            handle.write('"}')
        profile = detect(temp_workspace)
        assert profile.kind == "node"
        assert profile.run_command is None

    def test_malformed_json_is_survived(self, temp_workspace):
        with open(os.path.join(temp_workspace, "package.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        assert detect(temp_workspace).kind == "node"


class TestDetectOther:
    def test_detects_python(self, temp_workspace):
        make_python_project(temp_workspace, test_dir=True)
        profile = detect(temp_workspace)
        assert profile.kind == "python"
        assert profile.language == "python"
        assert profile.test_command == "pytest"

    def test_poetry_is_recognised(self, temp_workspace):
        with open(os.path.join(temp_workspace, "pyproject.toml"), "w", encoding="utf-8") as handle:
            handle.write('[tool.poetry]\nname = "demo"\n')
        assert detect(temp_workspace).framework == "poetry"

    def test_detects_rust(self, temp_workspace):
        open(os.path.join(temp_workspace, "Cargo.toml"), "w").close()
        profile = detect(temp_workspace)
        assert profile.kind == "rust"
        assert profile.test_command == "cargo test"

    def test_detects_go(self, temp_workspace):
        open(os.path.join(temp_workspace, "go.mod"), "w").close()
        assert detect(temp_workspace).test_command == "go test ./..."

    def test_maven_and_gradle(self, temp_workspace):
        open(os.path.join(temp_workspace, "pom.xml"), "w").close()
        assert detect(temp_workspace).package_manager == "maven"
        assert detect(temp_workspace).test_command == "mvn test"

    def test_a_mixed_project_is_fullstack(self, temp_workspace):
        make_python_project(temp_workspace)
        make_node_project(temp_workspace)
        profile = detect(temp_workspace)
        assert profile.kind == "fullstack"
        assert any("Multiple project types" in note for note in profile.notes)

    def test_an_unknown_directory_says_so(self, temp_workspace):
        profile = detect(temp_workspace)
        assert profile.kind == "unknown"
        assert profile.language is None

    def test_a_missing_directory_does_not_raise(self, temp_workspace):
        profile = detect(os.path.join(temp_workspace, "nope"))
        assert profile.kind == "unknown"
        assert "Not a directory" in profile.notes

    def test_source_directories_are_reported(self, temp_workspace):
        make_python_project(temp_workspace)
        os.makedirs(os.path.join(temp_workspace, "src"), exist_ok=True)
        assert "src" in detect(temp_workspace).entry_points

    def test_git_is_detected_without_spawning_git(self, temp_workspace):
        git = os.path.join(temp_workspace, ".git")
        os.makedirs(git, exist_ok=True)
        with open(os.path.join(git, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("ref: refs/heads/main\n")
        profile = detect(temp_workspace)
        assert profile.is_git is True
        assert profile.branch == "main"

    def test_a_detached_head_yields_a_short_sha(self, temp_workspace):
        git = os.path.join(temp_workspace, ".git")
        os.makedirs(git, exist_ok=True)
        with open(os.path.join(git, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("a" * 40)
        assert detect(temp_workspace).branch == "a" * 12

    def test_detection_is_cheap_on_a_big_tree(self, temp_workspace):
        """One shallow pass: a recursive walk here would be slower than the
        thing it is trying to save."""
        make_python_project(temp_workspace)
        deep = os.path.join(temp_workspace, "a", "b", "c", "d", "e", "f")
        os.makedirs(deep, exist_ok=True)
        for index in range(50):
            open(os.path.join(deep, f"file{index}.py"), "w").close()
        profile = detect(temp_workspace)
        assert profile.kind == "python"
        # Only `src`/`app`/`lib` are considered, and none exist here.
        assert profile.entry_points == []


class TestIsWithin:
    def test_a_child_is_within(self):
        assert is_within("/tmp/app", "/tmp/app/src") is True

    def test_the_root_itself_is_within(self):
        assert is_within("/tmp/app", "/tmp/app") is True

    def test_a_name_prefix_sibling_is_not_within(self):
        """`startswith` would say yes. That is the bug `commonpath` fixes."""
        assert is_within("/tmp/app", "/tmp/app_evil") is False

    def test_a_parent_is_not_within(self):
        assert is_within("/tmp/app/src", "/tmp/app") is False


class TestValidation:
    def test_a_relative_path_resolves_against_the_workspace(self, temp_workspace):
        os.makedirs(os.path.join(temp_workspace, "project"), exist_ok=True)
        assert validate_path("project") == os.path.realpath(
            os.path.join(temp_workspace, "project")
        )

    def test_an_escape_is_refused(self, temp_workspace):
        with pytest.raises(ProjectRejected) as caught:
            validate_path(os.path.join(temp_workspace, "..", ".."))
        assert "outside the workspace" in str(caught.value)
        assert temp_workspace in str(caught.value), "the message must name the boundary"

    def test_a_sibling_prefix_is_refused(self, temp_workspace):
        with pytest.raises(ProjectRejected):
            validate_path(os.path.join(temp_workspace + "_evil"))

    def test_an_empty_path_is_refused(self, temp_workspace):
        with pytest.raises(ProjectRejected):
            validate_path("   ")

    def test_a_file_is_not_a_project(self, temp_workspace):
        target = os.path.join(temp_workspace, "file.txt")
        open(target, "w").close()
        with pytest.raises(ProjectRejected, match="Not a directory"):
            validate_path(target)

    def test_a_missing_directory_is_refused(self, temp_workspace):
        with pytest.raises(ProjectRejected):
            validate_path("does/not/exist")


class TestRegistry:
    async def test_register_persists(self, temp_workspace, temp_core_db):
        make_node_project(temp_workspace, scripts={"dev": "vite"})
        project = await register(temp_workspace)

        assert project["id"]
        assert project["name"] == os.path.basename(temp_workspace)
        assert project["kind"] == "node"

        reloaded = await get(project["id"])
        assert reloaded is not None
        assert reloaded["run_command"] == "npm run dev"

    async def test_reopening_updates_rather_than_duplicates(self, temp_workspace, temp_core_db):
        """A random uuid per open would create a second row for the same
        directory — and two places showing different branches."""
        first = await register(temp_workspace)
        second = await register(temp_workspace)

        assert first["id"] == second["id"]
        assert len(await list_projects()) == 1

    async def test_project_id_is_stable(self):
        assert project_id_for("/tmp/demo") == project_id_for("/tmp/demo")
        assert project_id_for("/tmp/demo") != project_id_for("/tmp/other")

    async def test_get_by_path(self, temp_workspace, temp_core_db):
        await register(temp_workspace)
        found = await get_by_path(temp_workspace)
        assert found is not None

    async def test_list_pins_first(self, temp_workspace, temp_core_db):
        other = os.path.join(temp_workspace, "other")
        os.makedirs(other)
        pinned = await register(temp_workspace)
        await register(other)
        await set_pinned(pinned["id"], True)

        listed = await list_projects()
        assert listed[0]["id"] == pinned["id"]
        assert listed[0]["pinned"] is True

    async def test_pinned_only_filter(self, temp_workspace, temp_core_db):
        other = os.path.join(temp_workspace, "other")
        os.makedirs(other)
        pinned = await register(temp_workspace)
        await register(other)
        await set_pinned(pinned["id"], True)

        assert len(await list_projects(pinned_only=True)) == 1

    async def test_recent_is_newest_first(self, temp_workspace, temp_core_db):
        a = os.path.join(temp_workspace, "a")
        b = os.path.join(temp_workspace, "b")
        os.makedirs(a)
        os.makedirs(b)
        await register(a)
        await register(b)
        assert [p["name"] for p in await recent()][0] == "b"

    async def test_commands_can_be_corrected(self, temp_workspace, temp_core_db):
        project = await register(temp_workspace)
        await update_commands(project["id"], test_command="pnpm vitest")

        reloaded = await get(project["id"])
        assert reloaded["test_command"] == "pnpm vitest"

    async def test_correcting_one_command_leaves_the_others(self, temp_workspace, temp_core_db):
        make_node_project(temp_workspace, scripts={"dev": "vite", "test": "jest"})
        project = await register(temp_workspace)
        await update_commands(project["id"], test_command="vitest")

        reloaded = await get(project["id"])
        assert reloaded["test_command"] == "vitest"
        assert reloaded["run_command"] == "npm run dev"

    async def test_remove_forgets_but_keeps_the_directory(self, temp_workspace, temp_core_db):
        project = await register(temp_workspace)
        assert await remove(project["id"]) is True
        assert await get(project["id"]) is None
        assert os.path.isdir(temp_workspace)

    async def test_entry_points_survive_as_a_list(self, temp_workspace, temp_core_db):
        os.makedirs(os.path.join(temp_workspace, "src"))
        project = await register(temp_workspace)
        assert (await get(project["id"]))["entry_points"] == ["src"]

    async def test_a_pin_on_an_unknown_project_fails_quietly(self, temp_core_db):
        assert await set_pinned("no-such-project", True) is False

    async def test_removing_an_unknown_project_fails_quietly(self, temp_core_db):
        assert await remove("no-such-project") is False


class TestContextBlock:
    def test_no_project_means_no_block(self):
        assert build_context(None) == ""
        assert build_context({}) == ""

    async def test_the_block_names_the_project_and_path(self, temp_workspace, temp_core_db):
        project = await register(temp_workspace)
        block = build_context(project)
        assert os.path.basename(temp_workspace) in block
        assert temp_workspace in block

    def test_an_unrecognised_project_asks_rather_than_assumes(self):
        block = build_context({"name": "mystery", "path": "/tmp/mystery", "kind": "unknown"})
        assert "ask before assuming" in block

    def test_commands_are_labelled_as_detected_defaults(self):
        """So the model can correct a wrong one instead of obeying it."""
        block = build_context({
            "name": "demo", "path": "/tmp/demo", "kind": "node",
            "test_command": "npm test",
        })
        assert "detected defaults" in block

    def test_git_state_is_reported(self):
        block = build_context({
            "name": "demo", "path": "/tmp/demo", "kind": "node",
            "is_git": True, "branch": "feature/x",
        })
        assert "feature/x" in block

    def test_the_open_file_is_named(self):
        block = build_context(
            {"name": "demo", "path": "/tmp/demo", "kind": "node"},
            active_file="src/app.py",
        )
        assert "src/app.py" in block

    def test_the_block_is_capped(self):
        """A context block that does not fit a small model's window is not a
        context block."""
        block = build_context({
            "name": "x" * 200, "path": "/tmp/x", "kind": "node",
            "notes": ["n" * 5000],
        })
        assert len(block) <= 1200

    def test_context_from_path_detects_on_the_fly(self, temp_workspace):
        make_node_project(temp_workspace, scripts={"test": "vitest"})
        block = context_from_path(temp_workspace)
        assert "npm run test" in block

    def test_workspace_summary_lists_visible_entries(self, temp_workspace):
        open(os.path.join(temp_workspace, "README.md"), "w").close()
        os.makedirs(os.path.join(temp_workspace, ".hidden"))
        summary = workspace_summary(temp_workspace)
        assert "README.md" in summary
        assert ".hidden" not in summary

    def test_workspace_summary_of_an_empty_directory(self, temp_workspace):
        assert "is empty" in workspace_summary(temp_workspace)

    def test_workspace_summary_of_a_missing_directory(self, temp_workspace):
        assert workspace_summary(os.path.join(temp_workspace, "nope")) == ""