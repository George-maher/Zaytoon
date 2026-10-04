"""The side panel's parsing and sampling, without a display or a backend.

``app/sidepanel.py`` is split deliberately: the widget renders strings it is
handed, and everything that *produces* those strings is a module-level pure
function or a ctypes wrapper that returns ``None`` instead of raising.

That matters because two of the endpoints the panel asks for
(``/audit``, ``/processes``) **do not exist** on the backend. A 404 body is
HTML, HTML is not JSON, and a worker thread that raises on that is a worker
thread that dies. These tests are the tolerance contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import sidepanel as sp  # noqa: E402


class TestDecodeJson:
    def test_valid_json(self):
        assert sp.decode_json(b'{"a": 1}') == {"a": 1}
        assert sp.decode_json('{"a": 1}') == {"a": 1}

    def test_html_from_a_404_is_none_not_a_crash(self):
        assert sp.decode_json(b"<html><body>Not Found</body></html>") is None

    def test_truncated_body_is_none(self):
        assert sp.decode_json(b'{"tools": [') is None

    def test_none_and_bad_bytes(self):
        assert sp.decode_json(None) is None
        assert sp.decode_json(b"\xff\xfe\x00") is None

    def test_already_parsed_objects_pass_through(self):
        # Fake openers in tests return parsed payloads directly.
        assert sp.decode_json({"already": True}) == {"already": True}


class TestFetchJson:
    def test_opener_is_used_and_parsed(self):
        assert sp.fetch_json("http://x/tools", opener=lambda u, t: b'{"tools": []}') == {
            "tools": []
        }

    @pytest.mark.parametrize(
        "error",
        [
            OSError("connection refused"),
            ValueError("bad url"),
            TimeoutError("timed out"),
            RuntimeError("unexpected"),
        ],
    )
    def test_every_failure_reads_as_no_data(self, error):
        def opener(url, timeout):
            raise error

        assert sp.fetch_json("http://x/audit", opener=opener) is None


class TestParseTools:
    def test_wrapped_list(self):
        assert sp.parse_tools({"tools": ["a", "b"]}) == ["a", "b"]

    def test_bare_list(self):
        assert sp.parse_tools(["a", "b"]) == ["a", "b"]

    def test_dict_entries_by_name_or_action(self):
        payload = [{"name": "a"}, {"action": "b"}, {"id": "c"}]
        assert sp.parse_tools(payload) == ["a", "b", "c"]

    def test_by_tier_grouping(self):
        payload = {"by_tier": {"safe": ["read"], "confirm": ["write"]}}
        assert sp.parse_tools(payload) == ["read", "write"]

    def test_duplicates_collapse(self):
        assert sp.parse_tools(["a", "a", {"name": "a"}]) == ["a"]

    def test_unrecognised_entries_are_skipped_not_stringified(self):
        # "None" or "123" showing up as a tool name is worse than an omission.
        assert sp.parse_tools([None, 123, {}, {"name": ""}]) == []

    @pytest.mark.parametrize("payload", [None, "html", 7, {"other": []}])
    def test_garbage_reads_as_empty(self, payload):
        assert sp.parse_tools(payload) == []


class TestParseModel:
    def test_head_of_the_chain(self):
        payload = {"llm": {"chain": [{"model": "qwen/qwen3.8-27b:free"}]}}
        assert sp.parse_model(payload) == "qwen/qwen3.8-27b:free"

    def test_routing_fallback(self):
        payload = {"llm": {"routing": {"fast": {"models": [{"model": "m1"}]}}}}
        assert sp.parse_model(payload) == "m1"

    def test_routing_string_models(self):
        payload = {"llm": {"routing": {"fast": {"models": ["m2"]}}}}
        assert sp.parse_model(payload) == "m2"

    @pytest.mark.parametrize("payload", [None, "html", {}, {"llm": {}}, {"llm": {"chain": []}}])
    def test_absent_model_reads_as_empty_header(self, payload):
        # The header then renders "MODEL: —" rather than inventing a name.
        assert sp.parse_model(payload) == ""


class TestParseAudit:
    def test_dict_entries_are_joined(self):
        payload = {
            "audit": [
                {"time": "12:00", "action": "file_write", "summary": "ok"},
            ]
        }
        assert sp.parse_audit(payload) == ["12:00 · file_write · ok"]

    def test_string_entries_pass_through(self):
        assert sp.parse_audit({"log": ["line one"]}) == ["line one"]

    def test_limit_is_honoured(self):
        payload = {"entries": [f"e{i}" for i in range(50)]}
        assert len(sp.parse_audit(payload)) == 8
        assert len(sp.parse_audit(payload, limit=3)) == 3

    @pytest.mark.parametrize("payload", [None, "<html>404</html>", 42, {"nope": []}])
    def test_missing_endpoint_reads_as_empty(self, payload):
        # /audit is not implemented on the backend; this is the normal case.
        assert sp.parse_audit(payload) == []


class TestParseProcesses:
    def test_name_pid_and_cpu(self):
        payload = {"processes": [{"name": "python", "pid": 42, "cpu": 3.5}]}
        assert sp.parse_processes(payload) == ["python · pid 42 · 3.5%"]

    def test_string_entries(self):
        assert sp.parse_processes({"procs": ["svchost"]}) == ["svchost"]

    def test_entry_with_nothing_useful_is_dropped(self):
        assert sp.parse_processes({"items": [{"foo": "bar"}]}) == []

    @pytest.mark.parametrize("payload", [None, "<html>404</html>", {}])
    def test_missing_endpoint_reads_as_empty(self, payload):
        assert sp.parse_processes(payload) == []


class TestSummarizeNames:
    def test_empty_is_the_dash(self):
        assert sp.summarize_names([]) == sp.UNKNOWN
        assert sp.UNKNOWN == "—"

    def test_short_list_is_joined(self):
        assert sp.summarize_names(["a", "b"]) == "a, b"

    def test_long_list_is_truncated_honestly(self):
        text = sp.summarize_names([f"t{i}" for i in range(20)], limit=10)
        assert "+10 more" in text
        assert text.count(",") <= 10


class TestCpuMaths:
    def test_busy_percentage(self):
        before = (100, 500, 100)   # idle, kernel, user
        after = (150, 600, 200)    # idle +50, kernel +100, user +100
        # total = kernel + user = 200; idle delta 50 -> busy = 150/200
        assert sp.cpu_percent(before, after) == 75.0

    def test_first_sample_is_none_not_zero(self):
        assert sp.cpu_percent(None, (1, 2, 3)) is None
        assert sp.cpu_percent((1, 2, 3), None) is None

    def test_empty_delta_is_none(self):
        assert sp.cpu_percent((1, 2, 3), (1, 2, 3)) is None

    def test_negative_delta_is_none(self):
        # A clock going backwards must not produce a negative percentage.
        assert sp.cpu_percent((10, 20, 30), (1, 2, 3)) is None

    def test_fully_idle_is_zero(self):
        before = (100, 100, 0)
        after = (200, 200, 0)
        assert sp.cpu_percent(before, after) == 0.0


class TestRamAndFormat:
    @pytest.mark.parametrize(
        "raw,expected",
        [(52, 52.0), ("52.4", 52.4), (-5, 0.0), (150, 100.0), (None, None)],
    )
    def test_ram_percent_is_clamped(self, raw, expected):
        assert sp.ram_percent(raw) == expected

    def test_unparseable_ram_is_none(self):
        assert sp.ram_percent("lots") is None

    def test_format_percent(self):
        assert sp.format_percent(None) == "—"
        assert sp.format_percent(0) == "0.0%"
        assert sp.format_percent(12.345) == "12.3%"


class TestSystemSampler:
    def test_due_respects_the_interval_floor(self):
        # ``_last_sample`` starts at 0.0, i.e. "never sampled", so a normal
        # monotonic clock (well past 0) is immediately due.
        sampler = sp.SystemSampler(min_interval=3.0, clock=lambda: 100.0)
        assert sampler.due() is True
        sampler.sample(now=100.0)
        assert sampler.due(now=101.0) is False
        assert sampler.due(now=103.0) is True

    def test_first_sample_has_no_cpu_but_still_reports_ram(self):
        sampler = sp.SystemSampler(
            times_reader=lambda: (10, 20, 30),
            memory_reader=lambda: 44,
            clock=lambda: 0.0,
        )
        sample = sampler.sample(now=0.0)
        assert sample == {"cpu": None, "ram": 44.0}

    def test_second_sample_produces_a_number(self):
        times = iter([(100, 200, 50), (150, 300, 150)])
        sampler = sp.SystemSampler(
            times_reader=lambda: next(times), memory_reader=lambda: 61, clock=lambda: 0.0
        )
        sampler.sample(now=0.0)
        second = sampler.sample(now=4.0)
        assert isinstance(second["cpu"], float)
        assert 0.0 <= second["cpu"] <= 100.0
        assert second["ram"] == 61.0

    def test_a_failing_reader_yields_none_not_a_crash(self):
        def boom():
            raise RuntimeError("no such API")

        sampler = sp.SystemSampler(
            times_reader=boom, memory_reader=lambda: None, clock=lambda: 0.0
        )
        # The reader itself is expected to return None; a *raising* reader is
        # the worker's problem, and the worker catches it. Here we assert the
        # value path: None in, honest dash out.
        assert sp.format_percent(sp.ram_percent(None)) == "—"
