"""Target resolution: what "open X" and "find X" mean, and what they may touch.

The resolver is the one place that decides between a website, an installed
application, a file, a folder and an honest "several" or "nothing". Every
defect this file pins was a *decision* defect, not a typo:

* ``open YouTube`` reporting failure on a machine with no YouTube application —
  a fact about the resolver invented as a fact about the machine;
* a file search that could turn into a browser tab because the website table was
  consulted for a ``search`` request;
* a filename search with no edges, which is a whole-drive scan;
* containment tested with a string prefix, so ``Documents_old`` inherited
  ``Documents``' authority.

Nothing here opens anything, launches anything, or reads a file's contents: the
whole module is read-only by construction, and the tests assert that rather
than assume it.
"""

from __future__ import annotations

import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import targets  # noqa: E402


# ---------------------------------------------------------------------------
# Text and site resolution
# ---------------------------------------------------------------------------
class TestSiteLookup:
    @pytest.mark.parametrize(
        "name",
        [
            "youtube",
            "YouTube",
            "  YOUTUBE  ",
            "youtube.com",
            "youtube website",
            "يوتيوب",
            "يو تيوب",
        ],
    )
    def test_the_forms_people_say_all_resolve_to_the_same_site(self, name):
        assert targets.find_site_url(name) == "https://www.youtube.com/"

    def test_arabic_spelling_variants_fold(self):
        # ة→ه and ى→ي are the same folding the rest of the codebase applies.
        assert targets.find_site_url("جوجل") == "https://www.google.com/"
        assert targets.find_site_url("خرايط") == "https://www.google.com/maps"
        assert targets.find_site_url("خرائط") == "https://www.google.com/maps"

    def test_an_unknown_name_is_not_fuzzily_matched(self):
        assert targets.find_site_url("my report") is None
        assert targets.find_site_url("report.pdf") is None
        assert targets.find_site_url("") is None

    def test_every_site_value_is_https(self):
        for url in targets.KNOWN_SITES.values():
            assert url.startswith("https://"), url


class TestRequestParsing:
    @pytest.mark.parametrize(
        "text,query,intent",
        [
            ("open youtube", "youtube", "open"),
            ("Open YouTube.", "YouTube", "open"),
            ("please open youtube", "youtube", "open"),
            ("شغل كروم", "كروم", "open"),
            ("افتح يوتيوب", "يوتيوب", "open"),
            ("يا زيتون افتح يوتيوب", "يوتيوب", "open"),
            ("find report", "report", "search"),
            ("where is report.pdf", "report.pdf", "search"),
            ("دورلي على ملف اسمه report", "report", "search"),
            ("فين ملف report", "report", "search"),
            ("ابحث عن invoice", "invoice", "search"),
        ],
    )
    def test_a_spoken_request_becomes_a_query_and_an_intent(self, text, query, intent):
        assert targets.parse_request(text) == (query, intent)

    def test_a_search_verb_is_not_an_open_verb(self):
        query, intent = targets.parse_request("دورلي على ملف اسمه report")
        assert intent == "search"
        # The noun is the request; the name is what follows it.
        assert query == "report"


# ---------------------------------------------------------------------------
# Authorized roots and containment
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace and a user folder, with every other root suppressed.

    ``authorized_roots`` is a policy list, so a test that does not pin it reads
    whatever this machine happens to have (Downloads, OneDrive, a second
    drive). Pinning makes the assertions about the *policy*, not the machine.
    """
    ws = tmp_path / "workspace"
    docs = tmp_path / "Documents"
    ws.mkdir()
    docs.mkdir()
    monkeypatch.setattr(targets, "_windows_shell_folders", lambda: [str(docs)])
    monkeypatch.setattr(targets, "_config_roots", lambda: [])
    monkeypatch.setattr(targets, "_HOME_DIR_NAMES", ())
    monkeypatch.setattr("tools.sandbox.workspace_root", lambda: str(ws))
    return ws, docs


class TestAuthorizedRoots:
    def test_the_workspace_and_the_user_folders_are_roots(self, workspace):
        ws, docs = workspace
        roots = [os.path.realpath(r) for r in targets.authorized_roots()]
        assert os.path.realpath(ws) in roots
        assert os.path.realpath(docs) in roots

    def test_a_root_that_does_not_exist_is_dropped(self, workspace, tmp_path):
        roots = [os.path.realpath(r) for r in targets.authorized_roots()]
        assert os.path.realpath(tmp_path / "Nowhere") not in roots

    def test_configured_roots_are_included(self, workspace, tmp_path, monkeypatch):
        extra = tmp_path / "Extra"
        extra.mkdir()
        monkeypatch.setattr(targets, "_config_roots", lambda: [str(extra)])
        roots = [os.path.realpath(r) for r in targets.authorized_roots()]
        assert os.path.realpath(extra) in roots

    def test_a_path_inside_a_root_is_authorized(self, workspace):
        ws, _ = workspace
        inside = ws / "notes.txt"
        inside.write_text("x", encoding="utf-8")
        assert targets.is_authorized_path(str(inside)) is True

    def test_a_sibling_with_the_same_prefix_is_not_authorized(self, workspace, tmp_path):
        """``Documents_old`` must not inherit ``Documents``' authority.

        This is the exact shape a ``str.startswith`` check gets wrong, and the
        codebase has paid for that bug once already in ``tools/sandbox``.
        """
        _, docs = workspace
        sibling = tmp_path / (docs.name + "_old")
        sibling.mkdir()
        target = sibling / "secret.txt"
        target.write_text("x", encoding="utf-8")
        assert targets.is_authorized_path(str(target)) is False

    def test_traversal_out_of_a_root_is_not_authorized(self, workspace, tmp_path):
        ws, _ = workspace
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        assert targets.is_authorized_path(str(ws / ".." / "outside.txt")) is False

    def test_an_empty_path_is_never_authorized(self):
        assert targets.is_authorized_path("") is False


# ---------------------------------------------------------------------------
# The bounded filename search
# ---------------------------------------------------------------------------
class TestSearchPaths:
    def test_an_exact_name_is_found(self, tmp_path):
        (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
        results = targets.search_paths("report", roots=[str(tmp_path)])
        assert [r["name"] for r in results] == ["report.pdf"]
        assert results[0]["is_dir"] is False

    def test_a_partial_name_is_found(self, tmp_path):
        (tmp_path / "q3-report-2024.pdf").write_text("x", encoding="utf-8")
        results = targets.search_paths("report", roots=[str(tmp_path)])
        assert [r["name"] for r in results] == ["q3-report-2024.pdf"]

    def test_a_folder_is_reported_as_a_folder(self, tmp_path):
        (tmp_path / "reports").mkdir()
        results = targets.search_paths("reports", roots=[str(tmp_path)])
        assert results and results[0]["is_dir"] is True

    def test_the_search_is_depth_bounded(self, tmp_path):
        deep = tmp_path
        for level in range(4):
            deep = deep / f"d{level}"
        deep.mkdir(parents=True)
        (deep / "found-at-four.txt").write_text("x", encoding="utf-8")
        # A file one level deeper than the cap must not be reached.
        deepest = deep / "d4"
        deepest.mkdir()
        (deepest / "too-deep.txt").write_text("x", encoding="utf-8")

        names = {r["name"] for r in targets.search_paths("found-at-four", roots=[str(tmp_path)])}
        assert names == {"found-at-four.txt"}
        assert targets.search_paths("too-deep", roots=[str(tmp_path)]) == []

    def test_the_entry_budget_stops_a_pathological_tree(self, tmp_path):
        for i in range(50):
            (tmp_path / f"item-{i}.txt").write_text("x", encoding="utf-8")
        results = targets.search_paths(
            "item", roots=[str(tmp_path)], max_entries=5, limit=50
        )
        assert len(results) <= 5

    def test_file_contents_are_never_read(self, tmp_path, monkeypatch):
        (tmp_path / "report.pdf").write_text("secret contents", encoding="utf-8")

        def _explode(*args, **kwargs):  # pragma: no cover - only runs on a bug
            raise AssertionError("search_paths opened a file; it must read names only")

        monkeypatch.setattr(builtins, "open", _explode)
        results = targets.search_paths("report", roots=[str(tmp_path)])
        assert [r["name"] for r in results] == ["report.pdf"]

    def test_noise_directories_are_not_walked(self, tmp_path):
        (tmp_path / "report.txt").write_text("x", encoding="utf-8")
        noise = tmp_path / "node_modules"
        noise.mkdir()
        (noise / "report.txt").write_text("x", encoding="utf-8")
        results = targets.search_paths("report", roots=[str(tmp_path)])
        assert [r["path"] for r in results] == [str(tmp_path / "report.txt")]

    def test_unicode_names_are_matched(self, tmp_path):
        (tmp_path / "تقرير.pdf").write_text("x", encoding="utf-8")
        results = targets.search_paths("تقرير", roots=[str(tmp_path)])
        assert [r["name"] for r in results] == ["تقرير.pdf"]

    def test_a_unicode_name_with_spaces_is_matched(self, tmp_path):
        (tmp_path / "ملف المشروع.md").write_text("x", encoding="utf-8")
        results = targets.search_paths("ملف المشروع", roots=[str(tmp_path)])
        assert [r["name"] for r in results] == ["ملف المشروع.md"]

    def test_an_exact_hit_outranks_a_buried_weak_hit(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (nested / "my-report-2024.pdf").write_text("x", encoding="utf-8")
        (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
        results = targets.search_paths("report", roots=[str(tmp_path)])
        assert results[0]["name"] == "report.pdf"

    def test_an_empty_query_finds_nothing(self, tmp_path):
        (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
        assert targets.search_paths("   ", roots=[str(tmp_path)]) == []

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="this platform has no symlinks"
    )
    def test_a_directory_symlink_is_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret-report.txt").write_text("x", encoding="utf-8")
        root = tmp_path / "root"
        root.mkdir()
        try:
            os.symlink(outside, root / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("creating a symlink needs a privilege this account lacks")
        assert targets.search_paths("secret-report", roots=[str(root)]) == []


# ---------------------------------------------------------------------------
# Refused suffixes
# ---------------------------------------------------------------------------
class TestRefusedTargets:
    @pytest.mark.parametrize(
        "name",
        ["setup.exe", "run.bat", "macro.docm", "book.xlsm", "link.lnk", "page.url", "s.ps1"],
    )
    def test_a_code_executing_suffix_is_refused(self, name):
        assert targets.is_refused_open_target(name) is not None

    @pytest.mark.parametrize("name", ["report.pdf", "notes.txt", "photo.jpg", "song.mp3"])
    def test_an_ordinary_document_is_allowed(self, name):
        assert targets.is_refused_open_target(name) is None


# ---------------------------------------------------------------------------
# The single decision
# ---------------------------------------------------------------------------
class TestResolveOpenRequest:
    @pytest.fixture(autouse=True)
    def _no_real_apps(self, monkeypatch):
        """Default: no application is installed, so tests opt in explicitly.

        The seam is ``tools.applications.resolve_app_detail`` — the module
        ``core.targets`` imports lazily — rather than the boundary function
        itself, so the boundary's own failure handling is still exercised.
        """
        import tools.applications as applications

        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda query, **kwargs: {"target": None, "found": False, "via": "none"},
        )

    def test_youtube_opens_the_website_when_no_app_exists(self):
        result = targets.resolve_open_request("open youtube", roots=[])
        assert result["kind"] == "website"
        assert result["url"] == "https://www.youtube.com/"

    def test_arabic_youtube_opens_the_website(self):
        result = targets.resolve_open_request("افتح يوتيوب", roots=[])
        assert result["kind"] == "website"
        assert result["url"] == "https://www.youtube.com/"

    def test_an_installed_application_wins_over_its_website(self, monkeypatch):
        import tools.applications as applications

        monkeypatch.setattr(
            applications,
            "resolve_app_detail",
            lambda query, **kwargs: {
                "found": True,
                "target": "C:/Apps/YouTube.exe",
                "via": "shortcut",
            },
        )
        result = targets.resolve_open_request("open youtube", roots=[])
        assert result["kind"] == "app"
        assert result["target"] == "C:/Apps/YouTube.exe"

    def test_a_search_request_never_becomes_a_website(self, monkeypatch):
        """`find youtube` is a filename search; answering with a browser tab is
        a different request than the one the user made."""
        import tools.applications as applications

        def _explode(query, **kwargs):  # pragma: no cover - only runs on a bug
            raise AssertionError("a search request consulted the application resolver")

        monkeypatch.setattr(applications, "resolve_app_detail", _explode)
        result = targets.resolve_open_request("find youtube", roots=[])
        assert result["kind"] == "none"

    def test_an_explicit_file_inside_a_root_opens_that_file(self, tmp_path):
        report = tmp_path / "report.pdf"
        report.write_text("x", encoding="utf-8")
        result = targets.resolve_open_request("open report.pdf", roots=[str(tmp_path)])
        assert result["kind"] == "file"
        assert result["path"] == str(report)

    def test_an_explicit_folder_opens_that_folder(self, tmp_path):
        folder = tmp_path / "reports"
        folder.mkdir()
        result = targets.resolve_open_request("open reports", roots=[str(tmp_path)])
        assert result["kind"] == "folder"
        assert result["path"] == str(folder)

    def test_a_path_outside_every_root_is_refused_with_a_reason(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        root = tmp_path / "root"
        root.mkdir()
        result = targets.resolve_open_request(f"open {outside}", roots=[str(root)])
        assert result["kind"] == "none"
        assert "ZAYTOON_SEARCH_ROOTS" in result["reason"]

    def test_a_refused_suffix_is_reported_as_blocked_not_opened(self, tmp_path):
        (tmp_path / "setup.exe").write_text("x", encoding="utf-8")
        result = targets.resolve_open_request("open setup.exe", roots=[str(tmp_path)])
        assert result["kind"] == "blocked"
        assert "can execute code" in result["reason"]

    def test_one_exact_match_is_not_a_guess(self, tmp_path):
        (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
        (tmp_path / "my-report-2024.pdf").write_text("x", encoding="utf-8")
        result = targets.resolve_open_request("open report", roots=[str(tmp_path)])
        assert result["kind"] == "file"
        assert result["name"] == "report.pdf"

    def test_several_close_matches_ask_instead_of_choosing(self, tmp_path):
        (tmp_path / "report-q3.pdf").write_text("x", encoding="utf-8")
        (tmp_path / "report-q4.pdf").write_text("x", encoding="utf-8")
        result = targets.resolve_open_request("open report", roots=[str(tmp_path)])
        assert result["kind"] == "ambiguous"
        assert len(result["candidates"]) == 2
        assert "Ask which one" in result["reason"]

    def test_nothing_found_says_what_was_looked_for_and_where(self):
        result = targets.resolve_open_request("open zzzz-nothing", roots=[])
        assert result["kind"] == "none"
        assert "zzzz-nothing" in result["reason"]
        assert "workspace" in result["reason"]

    def test_an_empty_request_is_an_honest_none(self):
        assert targets.resolve_open_request("", roots=[])["kind"] == "none"

    @pytest.mark.parametrize(
        "question",
        [
            "what is the weather",
            "what time is it",
            "how are you",
            "ايه الاخبار",
            "مين ده",
        ],
    )
    def test_a_question_is_not_turned_into_a_file_search(self, question):
        """A question has no target. Falling through to the filename search
        answered "what is the weather" with eight unrelated Downloads files —
        a confident answer to a question nobody asked."""
        result = targets.resolve_open_request(question, roots=[])
        assert result["kind"] == "none"
        assert "question" in result["reason"]

    def test_a_polite_open_request_is_still_a_request(self):
        """A sentence can start with a question word and still carry a verb."""
        result = targets.resolve_open_request("can you open youtube", roots=[])
        assert result["kind"] == "website"

    def test_an_application_resolver_failure_is_not_a_crash(self, monkeypatch):
        import tools.applications as applications

        def _boom(query, **kwargs):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(applications, "resolve_app_detail", _boom)
        result = targets.resolve_open_request("open youtube", roots=[])
        # The website answer survives a broken application resolver.
        assert result["kind"] == "website"
