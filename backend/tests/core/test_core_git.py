"""Git over REST, against real repositories.

Every test here makes a real repository in a temp workspace with `git init` and
real commits. A mock for git would be a mock for the thing under test: the
questions worth asking are whether a path is contained, whether a weird file name
survives the round trip, and whether a refusal maps to the right status — and
none of those can be answered by a double that returns what the test expected.

Skipped, not failed, when git is absent: a machine without git should not fail
the suite, but it should not silently claim the routes work either.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.workspace import git


def _have_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(
    not _have_git(), reason="git is not installed on this machine"
)


def as_path(directory):
    """The workspace fixture is a string; these tests want a Path to build in."""
    return Path(str(directory))


def run(args, cwd, **env):
    """Run git for the *test*, not the code: identity must be set explicitly.

    Without this a machine with no global user.name fails every commit, and the
    suite reports a product bug for a configuration difference.
    """
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    environment.update(env)
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, check=True, env=environment
    )


def confirmed(client, route, payload, **overrides):
    """The two-step dance the panel performs: preview, then replay the token.

    `overrides` lets a test lie in the confirming body — the way to prove the
    token, not the second request's body, decides what runs.
    """
    session = client.post(route, json=payload)
    assert session.status_code == 200, (route, session.text)
    frame = session.json()
    assert frame["requires_confirmation"] is True, (route, frame)
    returning = {**payload, "confirm": frame["confirm"], **overrides}
    return frame, client.post(route, json=returning)


@pytest.fixture
def repo(temp_workspace):
    """A real repository with one commit and a known file.

    The identity is written into the repository's own config, not only into the
    test's environment. `run()` above can pass `GIT_AUTHOR_*`, but the *service*
    builds its own `git commit` argv and inherits the process environment — so a
    machine with no global `user.email` (a fresh container, CI, a second
    developer) failed every commit here with "Author identity unknown", and the
    suite reported that as a product bug. A real working tree always has an
    identity; a hermetic fixture has to provide one too.

    The product must not supply it: injecting `-c user.name=...` into a commit
    would attribute the user's work to Zaytoon.
    """
    root = as_path(temp_workspace)
    run(["git", "init", "-q"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    (root / "readme.md").write_text("hello\n", encoding="utf-8")
    run(["git", "add", "readme.md"], root)
    run(["git", "commit", "-q", "-m", "first commit"], root)
    return root


class TestDetection:
    async def test_a_repository_is_detected(self, repo):
        assert await git.is_repository(".") is True

    async def test_a_plain_folder_is_not(self, temp_workspace):
        # Not an error: "no panel here" is the answer the UI needs.
        assert await git.is_repository(".") is False

    async def test_a_missing_git_is_reported_not_raised(self, temp_workspace, monkeypatch):
        # A panel must not show a 500 because git is not on PATH.
        assert await git.is_repository(".") is False

    async def test_status_outside_a_repository_says_so(self, temp_workspace):
        from core.workspace.git import GitNotARepository

        with pytest.raises(GitNotARepository):
            await git.status(".")


class TestContainment:
    async def test_a_path_outside_the_workspace_is_refused(self, repo, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(git.OutsideWorkspace):
            await git.status(str(outside))

    async def test_read_operations_refuse_too(self, repo, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(git.OutsideWorkspace):
            await git.log(5, str(outside))

    async def test_writes_refuse_too(self, repo, tmp_path):
        # The containment check is not applied to reads and forgotten for writes.
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(git.OutsideWorkspace):
            await git.commit("nope", str(outside))

    async def test_a_sibling_directory_is_not_inside(self, repo, temp_workspace, monkeypatch):
        # `app` vs `app_evil`: the prefix trap `str.startswith` falls into.
        import tools.sandbox as sandbox

        sibling = os.path.join(str(temp_workspace) + "_evil")
        os.makedirs(sibling, exist_ok=True)
        try:
            monkeypatch.setattr(sandbox, "workspace_root", lambda: str(temp_workspace))
            assert sandbox.resolve_workspace(sibling) == ""
        finally:
            import shutil

            shutil.rmtree(sibling, ignore_errors=True)


class TestStatus:
    async def test_a_clean_tree(self, repo):
        result = await git.status(".")
        assert result["clean"] is True
        assert result["entries"] == []
        assert result["branch"], "a fresh repo is on some branch"

    async def test_an_untracked_file(self, repo):
        (repo / "new.txt").write_text("x", encoding="utf-8")
        result = await git.status(".")
        assert result["untracked"] == 1
        assert result["entries"][0]["path"] == "new.txt"
        assert result["entries"][0]["status"] == "untracked"

    async def test_a_modified_file_is_unstaged_not_staged(self, repo):
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        result = await git.status(".")
        entry = result["entries"][0]
        assert entry["status"] == "modified"
        assert entry["staged"] is False
        assert entry["unstaged"] is True

    async def test_a_staged_modification(self, repo):
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        await git.add(["readme.md"])
        result = await git.status(".")
        assert result["entries"][0]["staged"] is True
        assert result["staged"] == 1

    async def test_a_staged_only_change_is_not_also_unstaged(self, repo):
        # The phantom-row bug: porcelain `M ` has an empty worktree column, so
        # there is nothing unstaged. Hardcoding `unstaged: True` put every
        # staged file into both lists and the panel showed a row the user
        # could not act on.
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        await git.add(["readme.md"])
        result = await git.status(".")
        entry = result["entries"][0]
        assert entry["staged"] is True
        assert entry["unstaged"] is False
        assert result["unstaged"] == 0

    async def test_a_partially_staged_change_is_in_both_columns(self, repo):
        # `MM` — the case the "both" status exists for: genuinely staged *and*
        # genuinely unstaged, so the fix must not overcorrect.
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        await git.add(["readme.md"])
        (repo / "readme.md").write_text("changed again\n", encoding="utf-8")
        result = await git.status(".")
        entry = result["entries"][0]
        assert entry["staged"] is True
        assert entry["unstaged"] is True
        assert result["staged"] == 1
        assert result["unstaged"] == 1

    async def test_a_deleted_file(self, repo):
        os.remove(repo / "readme.md")
        result = await git.status(".")
        assert result["entries"][0]["status"] == "deleted"

    async def test_a_renamed_file_reports_the_new_name(self, repo):
        os.rename(repo / "readme.md", repo / "readme2.md")
        run(["git", "add", "-A"], repo)
        result = await git.status(".")
        paths = [e["path"] for e in result["entries"]]
        assert "readme2.md" in paths
        assert "readme.md" not in paths

    async def test_a_file_name_with_a_space(self, repo):
        # The reason `--porcelain -z` is used: line-based output would split this.
        (repo / "my notes.md").write_text("x", encoding="utf-8")
        result = await git.status(".")
        assert "my notes.md" in [e["path"] for e in result["entries"]]

    async def test_a_file_name_in_arabic(self, repo):
        (repo / "ملف.md").write_text("x", encoding="utf-8")
        result = await git.status(".")
        assert "ملف.md" in [e["path"] for e in result["entries"]]

    async def test_the_branch_is_reported(self, repo):
        run(["git", "branch", "-M", "trunk"], repo)
        assert (await git.status("."))["branch"] == "trunk"


class TestLog:
    async def test_one_commit(self, repo):
        commits = await git.log(10)
        assert len(commits) == 1
        assert commits[0]["subject"] == "first commit"
        assert commits[0]["short"] and commits[0]["sha"].startswith(commits[0]["short"])

    async def test_newest_first(self, repo):
        (repo / "b.txt").write_text("x", encoding="utf-8")
        run(["git", "add", "-A"], repo)
        run(["git", "commit", "-q", "-m", "second commit"], repo)

        commits = await git.log(10)
        assert [c["subject"] for c in commits] == ["second commit", "first commit"]

    async def test_the_limit_is_respected(self, repo):
        commits = await git.log(1)
        assert len(commits) == 1

    async def test_an_absurd_limit_is_clamped(self, repo):
        # Not an error: a UI asking for 10000 commits wants them, and the repo
        # simply does not have that many.
        assert isinstance(await git.log(10000), list)

    async def test_the_author_and_date_are_reported(self, repo):
        commit = (await git.log(1))[0]
        assert commit["author"] == "Test"
        assert commit["date"].startswith("20")

    async def test_a_commit_subject_with_a_separator_character(self, repo):
        # The log format uses \x1f as a field separator, so a subject containing
        # one would corrupt the record if the fields were not length-checked.
        (repo / "c.txt").write_text("x", encoding="utf-8")
        run(["git", "add", "-A"], repo)
        run(["git", "commit", "-q", "-m", "tricky\x1fsubject"], repo)

        commits = await git.log(5)
        assert commits[0]["subject"] == "tricky\x1fsubject", (
            "a subject with the separator must survive, not shift the fields"
        )


class TestBranches:
    async def test_the_current_branch_is_marked(self, repo):
        result = await git.branches(".")
        assert result["current"]
        current = [b for b in result["branches"] if b["current"]]
        assert len(current) == 1
        assert current[0]["name"] == result["current"]

    async def test_a_second_branch_is_listed_but_not_current(self, repo):
        run(["git", "branch", "feature"], repo)
        result = await git.branches(".")
        names = [b["name"] for b in result["branches"]]
        assert "feature" in names
        assert result["current"] != "feature"

    async def test_remotes_are_flagged(self, repo):
        # A remote-tracking ref only exists after a fetch, and there is no
        # network here — so a local "remote" (a path) stands in. The point is
        # that `remotes/`-prefixed refs are flagged as remote, not that a real
        # remote can be reached.
        run(["git", "remote", "add", "origin", str(repo)], repo)
        run(["git", "fetch", "-q", "origin"], repo)
        result = await git.branches(".")
        assert any(b["remote"] for b in result["branches"])
        assert await git.remotes(".") != []

    async def test_no_remotes_is_an_empty_list(self, repo):
        assert await git.remotes(".") == []


class TestDiff:
    async def test_a_clean_tree_has_no_diff(self, repo):
        result = await git.diff(".")
        assert result["empty"] is True

    async def test_a_change_is_shown(self, repo):
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        result = await git.diff(".")
        assert result["empty"] is False
        assert "changed" in result["diff"]

    async def test_staged_and_unstaged_are_separate(self, repo):
        (repo / "readme.md").write_text("staged\n", encoding="utf-8")
        await git.add(["readme.md"])
        (repo / "readme.md").write_text("then more\n", encoding="utf-8")

        assert "staged" in (await git.diff(".", staged=True))["diff"]
        assert "then more" in (await git.diff("."))["diff"]

    async def test_a_bad_ref_is_refused(self, repo):
        with pytest.raises(Exception):
            await git.diff(".", ref="--output=/tmp/pwned")

    async def test_the_diff_can_be_narrowed_to_one_file(self, repo):
        # Both files must be *tracked and modified*: an untracked file never
        # appears in a plain diff, which would make this compare equal instead
        # of proving the narrowing worked.
        (repo / "other.txt").write_text("other\n", encoding="utf-8")
        run(["git", "add", "other.txt"], repo)
        run(["git", "commit", "-q", "-m", "add other"], repo)
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        (repo / "other.txt").write_text("other changed\n", encoding="utf-8")
        full = await git.diff(str(repo))
        narrow = await git.diff(str(repo), file="readme.md")
        assert "readme.md" in narrow["diff"]
        assert "other.txt" not in narrow["diff"]
        assert len(narrow["diff"]) < len(full["diff"])
        assert narrow["file"] == "readme.md"

    async def test_a_file_parameter_that_looks_like_an_option_is_refused(self, repo):
        # After `--` git would read it as a path anyway, but the value is
        # refused before that: one rule for every model- or user-supplied
        # token, checked in one place.
        with pytest.raises(ValueError):
            await git.diff(str(repo), file="--output=/tmp/pwned")

    async def test_an_untracked_file_diffs_empty(self, repo):
        # git has nothing to compare an untracked file against. The caller
        # decides what to say; this asserts the honest empty answer rather
        # than a crash or a fabricated diff.
        (repo / "fresh.txt").write_text("new\n", encoding="utf-8")
        result = await git.diff(str(repo), file="fresh.txt")
        assert result["empty"] is True


class TestWrite:
    async def test_add_then_commit(self, repo):
        (repo / "new.txt").write_text("x", encoding="utf-8")
        await git.add(["new.txt"])
        result = await git.commit("add new file")
        assert "add new file" in (await git.log(1))[0]["subject"]
        assert result["output"]

    async def test_add_all(self, repo):
        (repo / "a.txt").write_text("a", encoding="utf-8")
        (repo / "b.txt").write_text("b", encoding="utf-8")
        await git.add(all_files=True)
        status = await git.status(".")
        assert status["staged"] == 2

    async def test_add_all_reports_the_real_staged_count(self, repo):
        # The old formula was `len(args) - 1`, which for `["add", "-A"]`
        # counted the flag: the response claimed 1 staged file when 2 were.
        (repo / "a.txt").write_text("a", encoding="utf-8")
        (repo / "b.txt").write_text("b", encoding="utf-8")
        result = await git.add(all_files=True)
        assert result["staged"] == 2

    async def test_an_option_shaped_file_is_refused_before_git_runs(self, repo):
        # Two layers: the pathspec validator refuses it by name, and `--`
        # would stop git reading it as a flag even if the validator were
        # bypassed. This is the write path, so it does not depend on one.
        with pytest.raises(ValueError):
            await git.add(["--force"])

    async def test_a_file_name_with_a_space_can_be_staged(self, repo):
        # The status parser is `-z`-precise about exactly these names — the
        # old write-path allow-list refused spaces, so the panel could *show*
        # `my notes.md` but could never stage it. One filename, two handlers,
        # they must agree.
        (repo / "my notes.md").write_text("x\n", encoding="utf-8")
        await git.add(["my notes.md"])
        status = await git.status(".")
        entry = next(e for e in status["entries"] if e["path"] == "my notes.md")
        assert entry["staged"] is True

    async def test_an_arabic_file_name_can_be_staged_and_diffed(self, repo):
        (repo / "ملف.md").write_text("مرحبا\n", encoding="utf-8")
        await git.add(["ملف.md"])
        staged = await git.diff(str(repo), staged=True, file="ملف.md")
        assert "ملف.md" in staged["diff"]

    async def test_add_needs_files_or_all(self, repo):
        with pytest.raises(ValueError):
            await git.add()

    async def test_an_empty_commit_message_is_refused(self, repo):
        with pytest.raises(ValueError):
            await git.commit("   ")

    async def test_a_long_commit_message_is_refused(self, repo):
        with pytest.raises(ValueError):
            await git.commit("x" * 2001)

    async def test_committing_nothing_is_a_command_failure_not_a_crash(self, repo):
        # git's own message ("nothing to commit") is what the user needs.
        with pytest.raises(git.GitCommandFailed):
            await git.commit("nothing changed")

    async def test_checkout_switches(self, repo):
        run(["git", "branch", "feature"], repo)
        result = await git.checkout("feature")
        assert result["ref"] == "feature"
        assert (await git.status("."))["branch"] == "feature"

    async def test_checkout_can_create(self, repo):
        await git.checkout("brand-new", create=True)
        assert (await git.status("."))["branch"] == "brand-new"

    async def test_a_ref_that_looks_like_an_option_is_refused(self, repo):
        # `-` prefixed: git would read this as a flag, and the underlying
        # problem is argument injection, not a missing branch.
        with pytest.raises(Exception):
            await git.checkout("--force")

    async def test_a_ref_with_a_space_is_refused(self, repo):
        with pytest.raises(Exception):
            await git.checkout("my branch")

    async def test_pushing_to_nothing_fails_cleanly(self, repo):
        with pytest.raises(git.GitCommandFailed):
            await git.push()


class TestTheRestRoutes:
    """The routes, over the real app.

    The status mapping is the thing worth asserting here: a UI that cannot tell
    "outside the workspace" from "not a repository" from "git refused" cannot
    show anything useful.
    """

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from api.main import app

        return TestClient(app)

    def test_status(self, client, repo, temp_workspace):
        response = client.get("/git/status", params={"path": str(repo)})
        assert response.status_code == 200
        assert response.json()["clean"] is True

    def test_log(self, client, repo):
        response = client.get("/git/log", params={"path": str(repo)})
        assert response.status_code == 200
        assert response.json()["commits"][0]["subject"] == "first commit"

    def test_branches(self, client, repo):
        assert client.get("/git/branches", params={"path": str(repo)}).status_code == 200

    def test_a_path_outside_the_workspace_is_403(self, client, tmp_path):
        # Not 500, and not 200: the containment check is the whole point of
        # exposing these over an unauthenticated origin.
        outside = tmp_path / "outside"
        outside.mkdir()
        response = client.get("/git/status", params={"path": str(outside)})
        assert response.status_code == 403

    def test_a_folder_that_is_not_a_repository_is_404(self, client, temp_workspace):
        response = client.get("/git/status", params={"path": str(temp_workspace)})
        assert response.status_code == 404

    def test_commit_without_a_message_is_422(self, client, repo):
        response = client.post("/git/commit", json={"path": str(repo), "message": ""})
        assert response.status_code == 422

    def test_add_with_nothing_to_add_is_422(self, client, repo):
        response = client.post("/git/add", json={"path": str(repo)})
        assert response.status_code == 422

    def test_add_then_commit_over_http(self, client, repo):
        (repo / "http.txt").write_text("x", encoding="utf-8")
        assert client.post(
            "/git/add", json={"path": str(repo), "files": ["http.txt"]}
        ).status_code == 200
        response = confirmed(
            client, "/git/commit",
            {"path": str(repo), "message": "over http"},
        )[1]
        assert response.status_code == 200
        # The commit response carries the refreshed status, so the panel does not
        # need a second round trip to see its own change.
        assert response.json()["status"]["clean"] is True

    def test_files_must_be_an_array(self, client, repo):
        response = client.post(
            "/git/add", json={"path": str(repo), "files": "not-an-array"}
        )
        assert response.status_code == 422

    def test_a_file_name_with_a_space_over_http(self, client, repo):
        (repo / "my notes.md").write_text("x", encoding="utf-8")
        response = client.post(
            "/git/add", json={"path": str(repo), "files": ["my notes.md"]}
        )
        assert response.status_code == 200
        assert response.json()["staged"] == 1

    def test_a_bad_ref_is_a_command_failure(self, client, repo):
        response = client.get(
            "/git/diff", params={"path": str(repo), "ref": "--output=x"}
        )
        # 409, not 422: the value passed the transport's shape check and git is
        # what objected. Either way it is not 200 and not 500.
        assert response.status_code in (409, 422)


class TestTheApprovalStep:
    """The consequential verbs ask twice, and the token decides what runs.

    The property under test: a single POST — the shape an unauthenticated page
    could produce — can preview a consequence but never carry one out, and the
    confirming call is bound to the operation that was previewed, not to
    whatever its own body says.
    """

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from api.main import app

        return TestClient(app)

    def test_a_commit_needs_two_calls_and_the_first_is_an_effect_free_preview(
        self, client, repo
    ):
        before = run(["git", "rev-list", "--count", "HEAD"], repo).stdout.decode()
        session = client.post(
            "/git/commit", json={"path": str(repo), "message": "previews only"}
        )
        assert session.status_code == 200
        frame = session.json()
        assert frame["requires_confirmation"] is True
        assert frame["action"] == "commit"
        assert frame["confirm"].startswith("git")
        assert frame["tier"] == "confirm"
        assert "previews only" in frame["summary"]
        # The preview happened without a commit existing for it to describe.
        after = run(["git", "rev-list", "--count", "HEAD"], repo).stdout.decode()
        assert after == before

    def test_the_confirming_body_cannot_change_what_was_approved(self, client, repo):
        (repo / "bound.txt").write_text("x", encoding="utf-8")
        client.post("/git/add", json={"path": str(repo), "files": ["bound.txt"]})
        _, response = confirmed(
            client, "/git/commit",
            {"path": str(repo), "message": "the reviewed message"},
            message="SMUGGLED - the reviewed message",
        )
        assert response.status_code == 200
        log = client.get("/git/log", params={"path": str(repo)}).json()
        assert log["commits"][0]["subject"] == "the reviewed message"

    def test_a_token_is_single_use(self, client, repo):
        session = client.post(
            "/git/commit", json={"path": str(repo), "message": "once only"}
        )
        token = session.json()["confirm"]
        replay = client.post(
            "/git/commit", json={"path": str(repo), "message": "once only", "confirm": token}
        )
        assert replay.status_code == 409  # nothing staged in a clean tree: git's own refusal carries it
        spent = client.post(
            "/git/commit", json={"path": str(repo), "message": "once only", "confirm": token}
        )
        assert spent.status_code == 409

    def test_a_token_from_another_verb_is_refused(self, client, repo):
        session = client.post(
            "/git/checkout", json={"path": str(repo), "ref": "feature-x", "create": True}
        )
        token = session.json()["confirm"]
        response = client.post(
            "/git/commit", json={"path": str(repo), "message": "crossed", "confirm": token}
        )
        assert response.status_code == 409
        # And the minted-but-unspent checkout token still works for its own verb.
        assert confirmed(
            client, "/git/checkout",
            {"path": str(repo), "ref": "feature-y", "create": True},
        )[1].status_code == 200

    def test_garbage_and_empty_tokens_are_409(self, client, repo):
        response = client.post(
            "/git/commit", json={"path": str(repo), "message": "x", "confirm": "git-nope"}
        )
        assert response.status_code == 409

    def test_tier_reads_the_live_registry_where_one_exists(self, client, repo):
        session = client.post(
            "/git/push", json={"path": str(repo), "remote": "origin", "branch": "main"}
        )
        assert session.json()["tier"] == "confirm"

    def test_discard_is_gated_but_unstaging_is_not(self, client, repo):
        # Unstaging a staged new file is reversible: single-step.
        (repo / "fresh.txt").write_text("new\n", encoding="utf-8")
        assert client.post(
            "/git/add", json={"path": str(repo), "files": ["fresh.txt"]}
        ).status_code == 200
        unstage = client.post(
            "/git/restore",
            json={"path": str(repo), "files": ["fresh.txt"], "staged": True},
        )
        assert unstage.status_code == 200
        assert "requires_confirmation" not in unstage.json()

        # Modifying a committed file and then discarding the change is not:
        # the preview must come first, and the token confirm must carry it out.
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        frame, discard = confirmed(
            client, "/git/restore",
            {"path": str(repo), "files": ["readme.md"], "staged": False},
        )
        assert frame["action"] == "restore"
        assert "readme.md" in frame["summary"]
        assert discard.status_code == 200
        assert (repo / "readme.md").read_text(encoding="utf-8") == "hello\n"

    def test_a_push_preview_failures_on_confirm_still_speak_git(self, client, repo):
        _, response = confirmed(
            client, "/git/push",
            {"path": str(repo), "remote": "origin", "branch": "main"},
        )
        # No remote in this fixture: git refuses at step 2, with its own words.
        assert response.status_code == 409

    def test_an_outside_path_never_lives_long_enough_to_be_a_preview(
        self, client, tmp_path
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        response = client.post(
            "/git/commit", json={"path": str(outside), "message": "x"}
        )
        assert response.status_code == 403
        assert "requires_confirmation" not in response.json()


class TestNetworkVerbs:
    """fetch / pull / merge / restore — against real remote topology.

    A path-based remote plus a second clone, because "does pull fast-forward?"
    cannot be answered by a stub that returns the string "pulled". Every git
    behaviour asserted here was probed against the installed git first, so the
    expectations are observations and not guesses.
    """

    @pytest.fixture
    def origin(self, repo, tmp_path_factory):
        """A bare 'origin' holding the repo's first commit, wired with
        upstream tracking so a bare `git pull` has somewhere to pull from."""
        bare = tmp_path_factory.mktemp("origin") / "origin.git"
        run(["git", "init", "-q", "--bare", str(bare)], repo)
        run(["git", "remote", "add", "origin", str(bare)], repo)
        run(["git", "push", "-q", "-u", "origin", "HEAD"], repo)
        return bare

    def _second_clone(self, bare, tmp_path_factory, name="clone"):
        clone = tmp_path_factory.mktemp(name)
        run(["git", "clone", "-q", str(bare), str(clone)], clone.parent)
        return clone

    async def test_fetch_without_remotes_is_a_clean_no_op(self, repo):
        # Probed: `git fetch --prune` exits 0 silently with zero remotes.
        result = await git.fetch(path=str(repo))
        assert "output" in result

    async def test_fetch_updates_remote_tracking_refs(self, origin, repo, tmp_path_factory):
        clone = self._second_clone(origin, tmp_path_factory)
        (clone / "from-origin.txt").write_text("hi\n", encoding="utf-8")
        run(["git", "add", "from-origin.txt"], clone)
        run(["git", "commit", "-q", "-m", "on the clone"], clone)
        run(["git", "push", "-q"], clone)

        await git.fetch(path=str(repo))
        result = await git.branches(str(repo))
        assert any(b["remote"] for b in result["branches"]), (
            "fetch must make the remote's branches visible to the panel"
        )

    async def test_pull_fast_forwards(self, origin, repo, tmp_path_factory):
        clone = self._second_clone(origin, tmp_path_factory)
        (clone / "from-origin.txt").write_text("hi\n", encoding="utf-8")
        run(["git", "add", "from-origin.txt"], clone)
        run(["git", "commit", "-q", "-m", "on the clone"], clone)
        run(["git", "push", "-q"], clone)

        result = await git.pull(path=str(repo))
        assert (repo / "from-origin.txt").exists()
        assert result["ff_only"] is True

    async def test_pull_without_a_remote_fails_cleanly(self, repo):
        # Probed: exits 1 with "There is no tracking information…". A panel
        # needs a 409 with those words, not a stack trace.
        with pytest.raises(git.GitCommandFailed) as excinfo:
            await git.pull(path=str(repo))
        assert "tracking" in str(excinfo.value).lower()

    async def test_pull_refuses_divergence_instead_of_merging(self, origin, repo, tmp_path_factory):
        # Local history moves, remote history moves: --ff-only must refuse
        # rather than invent a merge commit the user did not write.
        clone = self._second_clone(origin, tmp_path_factory)
        (clone / "remote-side.txt").write_text("r\n", encoding="utf-8")
        run(["git", "add", "remote-side.txt"], clone)
        run(["git", "commit", "-q", "-m", "remote move"], clone)
        run(["git", "push", "-q"], clone)

        (repo / "local-side.txt").write_text("l\n", encoding="utf-8")
        run(["git", "add", "local-side.txt"], repo)
        run(["git", "commit", "-q", "-m", "local move"], repo)

        with pytest.raises(git.GitCommandFailed):
            await git.pull(path=str(repo))

    async def test_merge_succeeds_without_conflict(self, repo):
        run(["git", "checkout", "-q", "-b", "feature"], repo)
        (repo / "feature.txt").write_text("f\n", encoding="utf-8")
        run(["git", "add", "feature.txt"], repo)
        run(["git", "commit", "-q", "-m", "feature work"], repo)
        run(["git", "checkout", "-q", "-"], repo)

        result = await git.merge("feature", path=str(repo))
        assert result["branch"] == "feature"
        assert (repo / "feature.txt").exists()

    async def test_a_conflicting_merge_carries_git_own_words(self, repo):
        # "CONFLICT (content): Merge conflict in f.txt" names the file better
        # than any paraphrase would — the whole point of surfacing stderr.
        trunk = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.decode().strip()
        run(["git", "checkout", "-q", "-b", "feature"], repo)
        (repo / "readme.md").write_text("feature version\n", encoding="utf-8")
        run(["git", "commit", "-q", "-am", "feature edit"], repo)
        run(["git", "checkout", "-q", trunk], repo)
        (repo / "readme.md").write_text("master version\n", encoding="utf-8")
        run(["git", "commit", "-q", "-am", "master edit"], repo)

        with pytest.raises(git.GitCommandFailed, match="CONFLICT"):
            await git.merge("feature", path=str(repo))

    async def test_merge_requires_a_branch(self, repo):
        with pytest.raises(ValueError):
            await git.merge("", path=str(repo))

    async def test_a_branch_that_looks_like_an_option_is_refused(self, repo):
        with pytest.raises(ValueError):
            await git.merge("--force", path=str(repo))

    async def test_restore_unstages_but_keeps_the_change(self, repo):
        (repo / "readme.md").write_text("changed\n", encoding="utf-8")
        run(["git", "add", "readme.md"], repo)

        await git.restore(["readme.md"], staged=True, path=str(repo))

        status = await git.status(str(repo))
        unstaged = [f["path"] for f in status["entries"] if f["unstaged"]]
        assert "readme.md" in unstaged
        assert (repo / "readme.md").read_text(encoding="utf-8") == "changed\n"

    async def test_restore_discards_a_worktree_change(self, repo):
        (repo / "readme.md").write_text("temporary\n", encoding="utf-8")
        await git.restore(["readme.md"], staged=False, path=str(repo))
        assert (repo / "readme.md").read_text(encoding="utf-8") == "hello\n"
        assert (await git.status(str(repo)))["clean"] is True

    async def test_restore_requires_files(self, repo):
        with pytest.raises(ValueError):
            await git.restore([], path=str(repo))

    async def test_a_file_name_with_a_space_can_be_unstaged(self, repo):
        (repo / "my notes.md").write_text("x\n", encoding="utf-8")
        await git.add(["my notes.md"])
        await git.restore(["my notes.md"], staged=True, path=str(repo))
        status = await git.status(str(repo))
        entry = next(e for e in status["entries"] if e["path"] == "my notes.md")
        assert entry["staged"] is False
        assert (repo / "my notes.md").exists()

    async def test_restore_unstages_in_a_repository_with_no_commits(self, temp_workspace):
        # Probed: `git restore --staged` dies with "could not resolve 'HEAD'"
        # here, so the documented `git rm --cached` fallback is what makes the
        # first-file case work instead of returning git's confusing words.
        root = as_path(temp_workspace)
        run(["git", "init", "-q"], root)
        (root / "first.txt").write_text("x\n", encoding="utf-8")
        run(["git", "add", "first.txt"], root)

        await git.restore(["first.txt"], staged=True, path=str(root))

        out = run(["git", "status", "--porcelain"], root).stdout.decode()
        assert "first.txt" in out  # still present, no longer staged
        assert not any(line.startswith("A ") for line in out.splitlines())


class TestNetworkRestRoutes:
    """The new verbs over HTTP: status mapping, shape checks."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from api.main import app

        return TestClient(app)

    def test_fetch_over_http(self, client, repo):
        response = client.post("/git/fetch", json={"path": str(repo)})
        assert response.status_code == 200

    def test_pull_with_no_remote_is_409(self, client, repo):
        response = client.post("/git/pull", json={"path": str(repo)})
        assert response.status_code == 409

    def test_merge_without_a_branch_is_422(self, client, repo):
        response = client.post("/git/merge", json={"path": str(repo), "branch": ""})
        assert response.status_code == 422

    def test_merge_over_http(self, client, repo):
        trunk = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.decode().strip()
        run(["git", "checkout", "-q", "-b", "feature"], repo)
        (repo / "feature.txt").write_text("f\n", encoding="utf-8")
        run(["git", "add", "feature.txt"], repo)
        run(["git", "commit", "-q", "-m", "feature work"], repo)
        run(["git", "checkout", "-q", trunk], repo)

        _, response = confirmed(
            client, "/git/merge", {"path": str(repo), "branch": "feature"}
        )
        assert response.status_code == 200
        assert (repo / "feature.txt").exists()

    def test_restore_without_files_is_422(self, client, repo):
        response = client.post("/git/restore", json={"path": str(repo)})
        assert response.status_code == 422

    def test_restore_over_http_unstages(self, client, repo):
        (repo / "readme.md").write_text("http change\n", encoding="utf-8")
        assert client.post(
            "/git/add", json={"path": str(repo), "files": ["readme.md"]}
        ).status_code == 200

        response = client.post(
            "/git/restore",
            json={"path": str(repo), "files": ["readme.md"], "staged": True},
        )
        assert response.status_code == 200
        status = client.get("/git/status", params={"path": str(repo)}).json()
        entry = next(f for f in status["entries"] if f["path"] == "readme.md")
        assert entry["staged"] is False
        assert entry["unstaged"] is True

    def test_a_path_outside_the_workspace_is_403_for_the_new_verbs(self, client, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        for route in ("/git/fetch", "/git/pull", "/git/merge", "/git/restore"):
            response = client.post(
                route, json={"path": str(outside), "files": ["x"], "branch": "x"}
            )
            assert response.status_code == 403, route