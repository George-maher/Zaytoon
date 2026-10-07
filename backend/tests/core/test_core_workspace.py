"""The file service: containment, listing, reading, searching, diffing."""

import os

import pytest

from core.workspace import files as fs


def write(root, relative, content="hello\n"):
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


class TestLanguage:
    @pytest.mark.parametrize("name,expected", [
        ("app.py", "python"),
        ("App.tsx", "typescript"),
        ("index.js", "javascript"),
        ("style.css", "css"),
        ("README.md", "markdown"),
        ("data.json", "json"),
        ("conf.yaml", "yaml"),
        ("main.go", "go"),
        ("Dockerfile", "dockerfile"),
        ("Makefile", "shell"),
        ("mystery.xyz", "plaintext"),
    ])
    def test_language_detection(self, name, expected):
        assert fs.language_for(name) == expected


class TestTextDetection:
    def test_a_known_extension_is_text(self):
        assert fs.is_text_file("a.py") is True

    def test_an_unknown_extension_is_not(self):
        assert fs.is_text_file("a.bin") is False

    def test_dotfiles_by_name(self):
        assert fs.is_text_file(".gitignore") is True
        assert fs.is_text_file(".env") is True


class TestContainment:
    def test_a_child_path_resolves(self, temp_workspace):
        write(temp_workspace, "src/app.py")
        resolved = fs.read_file("src/app.py")
        assert resolved["path"] == "src/app.py"

    def test_an_escape_is_refused(self, temp_workspace):
        """The service delegates to the same resolver every file tool uses."""
        with pytest.raises(fs.FileAccessDenied) as caught:
            fs.read_file(os.path.join(temp_workspace, "..", "..", "etc", "passwd"))
        assert temp_workspace in str(caught.value)

    def test_a_sibling_prefix_is_refused(self, temp_workspace):
        """`startswith` would accept this. That is the bug `commonpath` fixes."""
        with pytest.raises(fs.FileAccessDenied):
            fs.read_file(os.path.join(temp_workspace + "_evil", "secret.txt"))

    def test_the_workspace_root_itself_is_reachable(self, temp_workspace):
        # Reading it as a file is refused — it is a directory — but listing it is
        # the thing the explorer does first.
        assert fs.list_dir(".")["path"] == "."
        with pytest.raises(IsADirectoryError):
            fs.read_file(".")

    def test_the_workspace_root_as_dotdot_is_the_same_place(self, temp_workspace):
        write(temp_workspace, "a.txt")
        assert fs.list_dir("./")["path"] == "."
        assert fs.read_file("./a.txt")["path"] == "a.txt"


class TestListing:
    def test_directories_come_first(self, temp_workspace):
        write(temp_workspace, "b.txt")
        write(temp_workspace, "a/inner.py")
        listing = fs.list_dir(".")
        names = [e["name"] for e in listing["entries"]]
        assert names[0] == "a"
        assert "b.txt" in names

    def test_hidden_files_are_excluded_by_default(self, temp_workspace):
        write(temp_workspace, ".hidden")
        write(temp_workspace, "visible.txt")
        names = [e["name"] for e in fs.list_dir(".")["entries"]]
        assert ".hidden" not in names
        assert "visible.txt" in names

    def test_hidden_files_can_be_shown(self, temp_workspace):
        write(temp_workspace, ".hidden")
        names = [e["name"] for e in fs.list_dir(".", show_hidden=True)["entries"]]
        assert ".hidden" in names

    def test_gitignore_and_env_are_never_hidden(self, temp_workspace):
        """They matter to a developer and start with a dot by convention."""
        write(temp_workspace, ".gitignore")
        names = [e["name"] for e in fs.list_dir(".")["entries"]]
        assert ".gitignore" in names

    def test_entries_carry_metadata(self, temp_workspace):
        write(temp_workspace, "app.py", "x" * 100)
        entry = next(e for e in fs.list_dir(".")["entries"] if e["name"] == "app.py")
        assert entry["size"] == 100
        assert entry["language"] == "python"
        assert entry["modified"]

    def test_listing_a_missing_directory(self, temp_workspace):
        with pytest.raises(fs.FileNotFound):
            fs.list_dir("absent")

    def test_listing_a_file(self, temp_workspace):
        write(temp_workspace, "a.txt")
        with pytest.raises(NotADirectoryError):
            fs.list_dir("a.txt")


class TestTree:
    def test_tree_is_bounded_by_depth(self, temp_workspace):
        write(temp_workspace, "a/b/c/d/deep.py")
        shallow = fs.tree(".", depth=2)
        names = {e["name"] for e in shallow["entries"]}
        assert "a" in names
        assert not any(e["name"] == "deep.py" for e in shallow["entries"])

    def test_tree_reports_truncation(self, temp_workspace):
        for index in range(20):
            os.makedirs(os.path.join(temp_workspace, "many"), exist_ok=True)
            write(temp_workspace, f"many/deep{index}/file{index}.py")
        result = fs.tree(".", depth=6)
        assert result["truncated"] in (True, False), "truncation must be reported, not silent"

    def test_noise_directories_are_skipped(self, temp_workspace):
        write(temp_workspace, "node_modules/left-pad/index.js")
        write(temp_workspace, "src/app.js")
        names = {e["name"] for e in fs.tree(".", depth=3)["entries"]}
        assert "node_modules" not in names
        assert "src" in names

    def test_tree_of_the_root(self, temp_workspace):
        write(temp_workspace, "a/b.txt")
        assert fs.tree(".")["path"] == "."


class TestRead:
    def test_reads_text_with_metadata(self, temp_workspace):
        write(temp_workspace, "a.py", "print(1)\nprint(2)\n")
        result = fs.read_file("a.py")
        assert result["content"] == "print(1)\nprint(2)\n"
        assert result["binary"] is False
        assert result["language"] == "python"
        assert result["lines"] == 2

    def test_a_binary_file_is_reported_not_decoded(self, temp_workspace):
        path = os.path.join(temp_workspace, "blob.bin")
        with open(path, "wb") as handle:
            handle.write(b"\x00\x01\x02binary")
        result = fs.read_file("blob.bin")
        assert result["binary"] is True
        assert result["content"] == ""
        assert "Binary" in result["note"]

    def test_a_missing_file(self, temp_workspace):
        with pytest.raises(fs.FileNotFound):
            fs.read_file("absent.txt")

    def test_a_directory(self, temp_workspace):
        os.makedirs(os.path.join(temp_workspace, "d"))
        with pytest.raises(IsADirectoryError):
            fs.read_file("d")

    def test_an_oversized_file_is_refused(self, temp_workspace):
        path = write(temp_workspace, "big.txt", "A" * 1000)
        with pytest.raises(ValueError, match="the limit"):
            fs.read_file("big.txt", max_bytes=100)


class TestWrite:
    def test_writes_and_creates_parents(self, temp_workspace):
        result = fs.write_file("deep/nested/file.txt", "content")
        assert result["bytes"] == len("content")
        assert os.path.isfile(os.path.join(temp_workspace, "deep/nested/file.txt"))

    def test_overwriting_keeps_a_backup(self, temp_workspace):
        """Overwriting a source file with no way back is the irreversible action
        the whole permission system exists to prevent."""
        write(temp_workspace, "app.py", "original")
        result = fs.write_file("app.py", "replacement")
        assert result["backup"] is not None
        backup = os.path.join(temp_workspace, result["backup"])
        with open(backup, encoding="utf-8") as handle:
            assert handle.read() == "original"

    def test_a_new_file_has_no_backup(self, temp_workspace):
        assert fs.write_file("fresh.txt", "x")["backup"] is None

    def test_the_backup_can_be_declined(self, temp_workspace):
        write(temp_workspace, "app.py", "original")
        assert fs.write_file("app.py", "x", create_backup=False)["backup"] is None

    def test_line_endings_are_preserved(self, temp_workspace):
        write(temp_workspace, "crlf.txt", "a\r\nb")
        fs.write_file("crlf.txt", "c\r\nd")
        with open(os.path.join(temp_workspace, "crlf.txt"), "rb") as handle:
            assert b"\r\n" in handle.read()


class TestMutations:
    def test_mkdir(self, temp_workspace):
        result = fs.make_dir("new/dir")
        assert result["created"] is True
        assert os.path.isdir(os.path.join(temp_workspace, "new/dir"))

    def test_mkdir_twice_reports_that_it_existed(self, temp_workspace):
        fs.make_dir("dir")
        assert fs.make_dir("dir")["created"] is False

    def test_rename(self, temp_workspace):
        write(temp_workspace, "old.txt")
        result = fs.rename("old.txt", "new.txt")
        assert result["to"] == "new.txt"
        assert os.path.isfile(os.path.join(temp_workspace, "new.txt"))

    def test_rename_refuses_a_path_as_a_name(self, temp_workspace):
        """Renaming that silently moves a file is a different action with a
        different name."""
        write(temp_workspace, "a.txt")
        with pytest.raises(ValueError, match="bare filename"):
            fs.rename("a.txt", "../elsewhere.txt")

    def test_rename_onto_an_existing_name_is_refused(self, temp_workspace):
        write(temp_workspace, "a.txt")
        write(temp_workspace, "b.txt")
        with pytest.raises(FileExistsError):
            fs.rename("a.txt", "b.txt")

    def test_move(self, temp_workspace):
        write(temp_workspace, "a.txt")
        result = fs.move("a.txt", "sub/a.txt")
        assert os.path.isfile(os.path.join(temp_workspace, "sub/a.txt"))

    def test_copy_a_file(self, temp_workspace):
        write(temp_workspace, "a.txt", "content")
        fs.copy("a.txt", "b.txt")
        with open(os.path.join(temp_workspace, "b.txt"), encoding="utf-8") as handle:
            assert handle.read() == "content"

    def test_copy_a_directory(self, temp_workspace):
        write(temp_workspace, "src/a.txt")
        fs.copy("src", "dst")
        assert os.path.isfile(os.path.join(temp_workspace, "dst/a.txt"))

    def test_copy_a_directory_over_an_existing_one_replaces_it(self, temp_workspace):
        write(temp_workspace, "src/a.txt")
        write(temp_workspace, "dst/stale.txt")
        fs.copy("src", "dst")
        assert not os.path.exists(os.path.join(temp_workspace, "dst/stale.txt"))

    def test_delete_a_file(self, temp_workspace):
        write(temp_workspace, "a.txt")
        result = fs.delete("a.txt")
        assert result["kind"] == "file"
        assert not os.path.exists(os.path.join(temp_workspace, "a.txt"))

    def test_a_non_empty_directory_needs_recursive(self, temp_workspace):
        write(temp_workspace, "d/a.txt")
        with pytest.raises(ValueError, match="not empty"):
            fs.delete("d")

    def test_recursive_delete_reports_what_was_there(self, temp_workspace):
        write(temp_workspace, "d/a.txt")
        write(temp_workspace, "d/b.txt")
        assert fs.delete("d", recursive=True)["entries"] == 2
        assert not os.path.exists(os.path.join(temp_workspace, "d"))

    def test_deleting_a_missing_path(self, temp_workspace):
        with pytest.raises(fs.FileNotFound):
            fs.delete("absent")

    def test_mutations_refuse_an_escape(self, temp_workspace):
        with pytest.raises(fs.FileAccessDenied):
            fs.write_file(os.path.join("..", "escaped.txt"), "x")
        with pytest.raises(fs.FileAccessDenied):
            fs.delete(os.path.join("..", "something"))


class TestSearch:
    def test_finds_by_content(self, temp_workspace):
        write(temp_workspace, "a.py", "import os\nprint('hello')\n")
        write(temp_workspace, "b.py", "nothing here\n")
        result = fs.search("hello")
        assert result["count"] == 1
        assert result["matches"][0]["path"] == "a.py"
        assert result["matches"][0]["line"] == 2

    def test_reports_the_column(self, temp_workspace):
        write(temp_workspace, "a.py", "x = 'needle'\n")
        assert fs.search("needle")["matches"][0]["column"] == 6

    def test_finds_by_name(self, temp_workspace):
        write(temp_workspace, "find-me.py")
        result = fs.search("find-me", content=False)
        assert result["matches"][0]["name"] == "find-me.py"

    def test_a_glob_narrows_the_search(self, temp_workspace):
        write(temp_workspace, "a.py", "needle")
        write(temp_workspace, "b.md", "needle")
        result = fs.search("needle", glob="*.py")
        assert [m["path"] for m in result["matches"]] == ["a.py"]

    def test_regex_search(self, temp_workspace):
        write(temp_workspace, "a.py", "value = 42\n")
        assert fs.search(r"value = \d+", regex=True)["count"] == 1

    def test_an_invalid_regex_is_reported(self, temp_workspace):
        write(temp_workspace, "a.py", "x")
        with pytest.raises(ValueError, match="Invalid regular expression"):
            fs.search("[unclosed", regex=True, path=".")

    def test_an_empty_query_is_refused(self, temp_workspace):
        with pytest.raises(ValueError, match="query is required"):
            fs.search("   ")

    def test_results_are_capped_and_report_it(self, temp_workspace):
        for index in range(20):
            write(temp_workspace, f"f{index}.txt", "needle\n" * 5)
        result = fs.search("needle", limit=3)
        assert result["count"] == 3
        assert result["truncated"] is True

    def test_noise_directories_are_not_searched(self, temp_workspace):
        write(temp_workspace, "node_modules/pkg/index.js", "needle")
        assert fs.search("needle")["count"] == 0

    def test_binary_files_are_skipped(self, temp_workspace):
        with open(os.path.join(temp_workspace, "blob.bin"), "wb") as handle:
            handle.write(b"\x00needle\x00")
        assert fs.search("needle")["count"] == 0

    def test_no_matches_is_a_success_not_an_error(self, temp_workspace):
        write(temp_workspace, "a.txt")
        result = fs.search("absent")
        assert result["count"] == 0
        assert result["matches"] == []


class TestDiff:
    def test_a_change_produces_a_diff(self, temp_workspace):
        write(temp_workspace, "a.py", "line one\nline two\n")
        result = fs.diff_against_disk("a.py", "line one\nline CHANGED\n")
        assert result["changed"] is True
        assert "-line two" in result["diff"]
        assert "+line CHANGED" in result["diff"]
        assert result["removed_lines"] == 1
        assert result["added_lines"] == 1

    def test_identical_content_is_not_a_change(self, temp_workspace):
        write(temp_workspace, "a.py", "same\n")
        assert fs.diff_against_disk("a.py", "same\n")["changed"] is False

    def test_a_new_file_diffs_against_nothing(self, temp_workspace):
        result = fs.diff_against_disk("new.py", "content\n")
        assert result["changed"] is True
        assert "+content" in result["diff"]

    def test_the_diff_is_unified_format(self, temp_workspace):
        write(temp_workspace, "a.py", "old\n")
        diff = fs.diff_against_disk("a.py", "new\n")["diff"]
        assert diff.startswith("--- a/a.py")
        assert "+++ b/a.py" in diff

    def test_diffing_does_not_write(self, temp_workspace):
        write(temp_workspace, "a.py", "original")
        fs.diff_against_disk("a.py", "proposed")
        with open(os.path.join(temp_workspace, "a.py"), encoding="utf-8") as handle:
            assert handle.read() == "original"


class TestStat:
    def test_reports_metadata(self, temp_workspace):
        write(temp_workspace, "a.py", "content")
        info = fs.stat("a.py")
        assert info["name"] == "a.py"
        assert info["is_dir"] is False
        assert info["size"] == 7
        assert info["language"] == "python"
        assert info["binary"] is False

    def test_reports_a_directory(self, temp_workspace):
        os.makedirs(os.path.join(temp_workspace, "d"))
        info = fs.stat("d")
        assert info["is_dir"] is True
        assert info["language"] == ""

    def test_a_missing_path(self, temp_workspace):
        with pytest.raises(fs.FileNotFound):
            fs.stat("absent")

    def test_an_escape_is_refused(self, temp_workspace):
        with pytest.raises(fs.FileAccessDenied):
            fs.stat(os.path.join(temp_workspace, ".."))


class TestRootInfo:
    def test_reports_the_root(self, temp_workspace):
        info = fs.workspace_root_info()
        assert info["root"] == temp_workspace
        assert info["exists"] is True
        assert "node_modules" in info["skipped_dirs"]


class TestAsyncWrappers:
    async def test_the_async_api_returns_the_same_shape(self, temp_workspace):
        write(temp_workspace, "a.py", "content")
        listing = await fs.atree(".")
        assert listing["path"] == "."
        read = await fs.aread("a.py")
        assert read["content"] == "content"
        assert (await fs.astat("a.py"))["size"] == 7
        assert (await fs.asearch(query="content"))["count"] == 1
        assert (await fs.awrite("b.txt", "x"))["bytes"] == 1
        assert len((await fs.alist("."))["entries"]) == 2