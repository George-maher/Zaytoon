"""Tests for the local file/application indexer (app/indexer.py).

The indexer scans configured roots, applies exclusion patterns, and answers
search queries. Tests use tmp_path so nothing depends on the real filesystem.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.indexer import Indexer  # noqa: E402


_DEFAULT_EXCLUSIONS = [".*", "__pycache__", "node_modules", ".git", ".venv", "*.pyc", "*.db", "*.sqlite", "*.log", "*.tmp"]


class _Cfg:
    """Minimal config surface the indexer reads."""

    def __init__(self, index_roots=None, index_exclusions=None) -> None:
        self.index_roots = list(index_roots or [])
        self.index_exclusions = list(index_exclusions or _DEFAULT_EXCLUSIONS)


@pytest.fixture
def indexed_tree(tmp_path):
    """A realistic directory tree with files, subdirs, and excluded items."""
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "readme.md").write_text("# hello", encoding="utf-8")
    (root / "docs" / "notes.txt").write_text("notes", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "main.cpython-11.pyc").write_text("x", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.txt").write_text("shh", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.js").write_text("// lib", encoding="utf-8")
    (root / "archive.db").write_text("data", encoding="utf-8")
    return root


@pytest.fixture
def indexer_with_roots(indexed_tree, monkeypatch):
    """An indexer pointed at the tmp tree with default exclusions."""
    cfg = _Cfg(index_roots=[str(indexed_tree)])
    return Indexer(cfg)


class TestIndexerBuild:
    def test_rebuild_returns_item_count(self, indexer_with_roots, indexed_tree):
        count = indexer_with_roots.rebuild()
        assert count > 0
        assert indexer_with_roots._built

    def test_rebuild_finds_files_in_root(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        paths = {i.path for i in indexer_with_roots._items}
        assert str(indexed_tree / "docs" / "readme.md") in paths
        assert str(indexed_tree / "src" / "main.py") in paths

    def test_rebuild_finds_folders(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        kinds = {i.path: i.kind for i in indexer_with_roots._items}
        assert kinds[str(indexed_tree / "docs")] == "folder"
        assert kinds[str(indexed_tree / "src")] == "folder"

    def test_rebuild_is_idempotent(self, indexer_with_roots):
        first = indexer_with_roots.rebuild()
        second = indexer_with_roots.rebuild()
        assert first == second
        assert len(indexer_with_roots._items) == first


class TestExclusions:
    def test_excluded_hidden_directory_is_not_indexed(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        paths = {i.path for i in indexer_with_roots._items}
        assert str(indexed_tree / ".hidden" / "secret.txt") not in paths

    def test_excluded_pycache_is_not_indexed(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        paths = {i.path for i in indexer_with_roots._items}
        assert str(indexed_tree / "src" / "__pycache__" / "main.cpython-11.pyc") not in paths

    def test_excluded_node_modules_is_not_indexed(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        paths = {i.path for i in indexer_with_roots._items}
        assert str(indexed_tree / "node_modules" / "pkg.js") not in paths

    def test_excluded_database_is_not_indexed(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        paths = {i.path for i in indexer_with_roots._items}
        assert str(indexed_tree / "archive.db") not in paths

    def test_custom_exclusion_pattern(self, indexed_tree):
        cfg = _Cfg(
            index_roots=[str(indexed_tree)],
            index_exclusions=[".*", "__pycache__", "node_modules", "*.db", "*.md"],
        )
        idx = Indexer(cfg)
        idx.rebuild()
        paths = {i.path for i in idx._items}
        assert str(indexed_tree / "docs" / "readme.md") not in paths
        assert str(indexed_tree / "docs" / "notes.txt") in paths

    def test_exclusion_applies_to_directory_names_not_full_path(self, indexed_tree):
        """A directory named 'node_modules' anywhere is excluded."""
        cfg = _Cfg(index_roots=[str(indexed_tree)])
        idx = Indexer(cfg)
        idx.rebuild()
        paths = {i.path for i in idx._items}
        # node_modules itself should not appear as a folder
        assert str(indexed_tree / "node_modules") not in paths


class TestSearch:
    def test_search_returns_matching_items(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        results = indexer_with_roots.search("main")
        assert len(results) == 1
        assert results[0]["name"] == "main.py"

    def test_search_is_case_insensitive(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        assert len(indexer_with_roots.search("README")) == 1
        assert len(indexer_with_roots.search("readme")) == 1

    def test_search_returns_empty_for_no_match(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        assert indexer_with_roots.search("zzz_nonexistent") == []

    def test_search_returns_all_when_query_empty(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        total = len(indexer_with_roots._items)
        assert len(indexer_with_roots.search("")) == total

    def test_search_respects_limit(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        results = indexer_with_roots.search("", limit=3)
        assert len(results) == 3

    def test_get_by_id(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        item = indexer_with_roots.get(str(indexed_tree / "docs" / "readme.md"))
        assert item is not None
        assert item["name"] == "readme.md"
        assert item["kind"] == "file"
        assert item["mime_type"] == "text/markdown"

    def test_get_unknown_id_returns_none(self, indexer_with_roots):
        indexer_with_roots.rebuild()
        assert indexer_with_roots.get("/nonexistent/path") is None


class TestMimeTypes:
    def test_markdown_mime(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        item = indexer_with_roots.get(str(indexed_tree / "docs" / "readme.md"))
        assert item["mime_type"] == "text/markdown"

    def test_python_mime(self, indexer_with_roots, indexed_tree):
        indexer_with_roots.rebuild()
        item = indexer_with_roots.get(str(indexed_tree / "src" / "main.py"))
        assert item["mime_type"] == "text/x-python"

    def test_unknown_extension_has_no_mime(self, indexed_tree):
        (indexed_tree / "weird.xyz").write_text("x", encoding="utf-8")
        cfg = _Cfg(index_roots=[str(indexed_tree)])
        idx = Indexer(cfg)
        idx.rebuild()
        item = idx.get(str(indexed_tree / "weird.xyz"))
        assert item["mime_type"] is None


class TestMultipleRoots:
    def test_indexes_multiple_roots(self, tmp_path):
        root1 = tmp_path / "one"
        root1.mkdir()
        (root1 / "a.txt").write_text("a", encoding="utf-8")
        root2 = tmp_path / "two"
        root2.mkdir()
        (root2 / "b.txt").write_text("b", encoding="utf-8")

        cfg = _Cfg(index_roots=[str(root1), str(root2)])
        idx = Indexer(cfg)
        idx.rebuild()
        names = {i.name for i in idx._items}
        assert "a.txt" in names
        assert "b.txt" in names
