"""A swallowed failure still has to leave a trace.

`except Exception: pass` appears 40 times in the shipped package. Most of them
are *correct* decisions — instrumentation must not fail the call it measures, a
temp-file cleanup must not change a transcription result — and those stay. What
was wrong is that "correctly swallowed" and "nobody knows this broke" produced
the same observable behaviour: nothing.

Two of them were also not harmless. `SessionState.cancel()` swallowed a failure
from `confirmation_manager.cancel_all()`, which is the call that stops a dead
session's pending approvals from being honoured later; and
`_load_tool_shapes()` swallowed an error while building the tool map, so the
completion validator kept reporting "no violations" about a contract with no
tools in it.

These tests pin the property, not the wording: when the failure path fires, a
log record must exist *and* the best-effort caller must still get its result.
"""

from __future__ import annotations

import ast
import logging
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: (module, the failure it used to hide). Every entry here is a swallow that was
#: replaced with `common.reporting.safe_error` (or an explicit log call) because
#: the invisible version was indistinguishable from "nothing was wrong".
REPORTED_SWALLOWS = [
    "tools/metrics.py",
    "services/completion_validator.py",
    "core/tasks/manager.py",
    "core/secrets.py",
    "domain/models.py",
    "voice/stt.py",
    "core/orchestrator.py",
    "agents/engine.py",
    "api/websocket.py",
    "api/main.py",
    "scheduler/manager.py",
]


#: Exceptions whose swallow is *expected*, because the only thing that can land
#: here is the interpreter unwinding a cancelled task.
_ROUTINE = {"CancelledError", "KeyboardInterrupt"}


def _silent_swallow_calls(tree: ast.AST) -> int:
    """Count **broad** silent swallows in a module.

    Deliberately narrow about what counts: `except FileNotFoundError: pass` in
    "read the secrets file if it exists" is a decision, not a defect, and a guard
    that flags it teaches people to delete the guard. What is forbidden is a
    handler that can catch `Exception` (or everything, bare) and then say
    nothing — the shape where a real bug is indistinguishable from a clean run.
    """
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Pass):
            continue
        caught = {
            part.strip().rsplit(".", 1)[-1]
            for part in ast.unparse(node.type).split(",")
        } if not isinstance(node.type, ast.Tuple) else {
            ast.unparse(e).strip().rsplit(".", 1)[-1] for e in node.type.elts
        }
        if caught <= _ROUTINE:
            continue
        if caught & {"Exception", "BaseException"}:
            count += 1
    return count


class TestNoLongerSilent:
    @pytest.mark.parametrize("relpath", REPORTED_SWALLOWS)
    def test_the_named_module_reports_the_failures_it_tolerates(self, relpath):
        """A broad `except Exception: pass` in these files is a regression.

        Asserted structurally because the point is the *absence* of a behaviour:
        there is nothing to call that would show it. The count is per file rather
        than per line, so a refactor that moves the handler cannot dodge it by
        relocating the code.
        """
        path = BACKEND / relpath
        if not path.exists():  # pragma: no cover - a typo in the table above
            pytest.fail(f"{relpath} does not exist - the table has gone stale")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        silent = _silent_swallow_calls(tree)
        assert silent == 0, (
            f"{relpath} swallows {silent} broad exception(s) with no report; "
            "use common.reporting.safe_error, which cannot itself raise"
        )


#: The backend package root, so the table above can name modules the way the
#: rest of the codebase does.
BACKEND = pathlib.Path(__file__).resolve().parent.parent


class _Boom(Exception):
    pass


class TestMetricsStillCannotBreakACall:
    def test_a_broken_recorder_is_reported_and_the_call_survives(self, caplog):
        from tools.metrics import record_tool_call

        class Broken:
            def end(self, *a, **k):
                raise _Boom("counter overflow")

        with caplog.at_level(logging.DEBUG, logger="Zaytoon"):
            record_tool_call(Broken(), "run_command", status="ok", duration_ms=1.0)

    def test_the_report_actually_happens(self, caplog):
        """Same call, but asserting the observable: a record was emitted.

        Split from the test above on purpose — "it did not raise" and "it said
        something" are two different properties, and the first one passing is
        exactly what let the second one be forgotten.
        """
        from tools.metrics import record_tool_call

        class Broken:
            def end(self, *a, **k):
                raise _Boom("counter overflow")

        with caplog.at_level(logging.WARNING, logger="Zaytoon"):
            record_tool_call(Broken(), "run_command", status="ok", duration_ms=1.0)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "metrics.end" in text, text


class TestCancellationCompletes:
    def test_a_failing_confirm_manager_does_not_stop_the_cancel(self, caplog):
        """`cancel_event` must be set even when the manager explodes.

        Before this there was no log *and* no way to tell whether the
        confirmation side of the cleanup ran at all — the one failure mode where
        silence is genuinely dangerous, because a pending approval for a dead
        session could still be answered and honoured.
        """
        from domain.models import SessionState

        state = SessionState()

        class Dead:
            def cancel_all(self, reason):
                raise _Boom("manager is gone")

        state.confirmation_manager = Dead()
        with caplog.at_level(logging.ERROR, logger="Zaytoon"):
            state.cancel()

        assert state.cancel_event.is_set(), "cancellation itself must complete"
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "NOT cancelled" in text, text
        assert any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), "a security-relevant failure does not belong at debug level"


class TestValidatorReportsItsOwnFailure:
    def test_a_broken_tool_is_visible_in_the_log(self, caplog):
        """An empty tool map used to mean "validated, nothing wrong"."""
        from services.completion_validator import contract_from_registry

        class BadTool:
            """Introspectable enough to be listed, broken enough to explode."""

            name = "bad"
            description = "raises on introspection"
            schema = {}

            def __getattr__(self, item):
                raise _Boom(f"cannot read {item}")

        class Registry:
            def tools(self):
                return [BadTool()]

        with caplog.at_level(logging.WARNING, logger="Zaytoon"):
            contract = contract_from_registry(Registry())
        assert contract.tools == {}
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "completion_validator.load_shapes" in text, text


class TestSchedulerSurvivesAStoreThatThrows:
    async def test_requeue_failure_does_not_stop_the_scheduler(self, monkeypatch, caplog):
        from reminder.service import ReminderService
        from scheduler.manager import SchedulerManager

        class Memory:
            async def requeue_stale_claims(self):
                raise _Boom("db locked")

            async def get_due_reminders(self):
                return []

        service = ReminderService(Memory())
        manager = SchedulerManager(service, None)
        monkeypatch.setattr(manager, "_check_interval", 0, raising=False)
        with caplog.at_level(logging.WARNING, logger="Zaytoon"):
            await manager.start()
            await manager.stop()
        assert "scheduler.requeue" in "\n".join(r.getMessage() for r in caplog.records)
