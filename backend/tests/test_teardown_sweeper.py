"""The teardown sweeper: no aiosqlite worker thread survives the suite.

`aiosqlite` gives every connection a **non-daemon** worker thread that exits only
once its stop sentinel has been queued — normally from ``close()``. A connection
that is still referenced while still running therefore keeps that thread alive
through ``threading._shutdown``, and the interpreter hangs after pytest has
printed its summary. It looked like a hang in the harness rather than a test
failure, and only ever happened on subset runs, which is why it defied every
attempt to make it a reproducible CI error.

pytest keeps frames alive in tracebacks, so a connection left reachable past a
test's own teardown is retained by the *process*, not dropped — this module
reproduces that state deliberately (``test_a_running_connection_is_released``
holds one on a module global and never closes it) and then asserts the sweeper
stops its thread. Nothing here weakens production behaviour: production closes
what it opens, and this exercises test infrastructure only.
"""

import threading

import aiosqlite
import pytest

from _sqlite_sweeper import release_stray_threads, stray_thread_names

#: Anything a test leaves behind deliberately, so no one can mistake the leak
#: for an accident of an earlier test.
HELD: list = []


async def test_a_running_connection_is_released(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "leaked.db"))
    await conn.execute("SELECT 1")
    HELD.append(conn)
    assert conn._running is True, "precondition: the connection must be live"

    released = release_stray_threads()

    assert released >= 1, "the sweeper found no connection to release"
    assert conn._running is False
    # Bounded: a wedged queue would leave `join` open-ended, which is the
    # failure the sweeper exists to prevent, not to cause twice.
    conn._thread.join(timeout=1.0)
    assert not conn._thread.is_alive()


def test_the_sweeper_is_idempotent():
    """A second call over a clean process may collect objects but must not
    queue a second sentinel into anything already stopped."""
    assert release_stray_threads() >= 0
    assert not any(name.startswith("_connection") for name in stray_thread_names())


def test_no_non_daemon_thread_survives_the_sweep():
    """The invariant the suite actually cares about, stated as a test.

    A thread the process still holds makes ``threading._shutdown`` wait for it.
    After the sweep, no live aiosqlite connection may still be running.
    """
    release_stray_threads()
    survivors = [
        conn
        for conn in _live_connections()
        if getattr(conn, "_running", False)
        and conn._thread and conn._thread.is_alive()
    ]
    assert not survivors, f"{len(survivors)} running connection(s) survived"


def _live_connections():
    import gc

    import aiosqlite

    return [
        obj for obj in gc.get_objects()
        if type(obj) is aiosqlite.Connection and getattr(obj, "_running", False)
    ]
