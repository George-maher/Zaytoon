"""Release aiosqlite worker threads a test left behind. **Test-only.**

`aiosqlite` gives every connection a **non-daemon** worker thread that exits
only once :meth:`Connection.stop` has queued its sentinel — which normally
happens inside :meth:`~Connection.close`. The suite's production code closes
what it opens, but a *test* can leave a connection referenced after its own
event loop is gone: pytest keeps frames alive in tracebacks, so a failure or a
call mid-connect can outlive the test that made it.

Then the interpreter sits in ``threading._shutdown`` after pytest has printed
its summary, waiting for a thread that will never be joined — minutes of dead
silence that read exactly like a hang in the harness, and which only ever showed
up on *subset* runs. This was verified pre-existing, and reproduced
deterministically by retaining one running connection past the end of a run.

The sweeper is a last resort, not a replacement for closing connections: it
mops up only connections still ``_running`` at session finish, and it does it
with :meth:`Connection.stop` rather than an awaited ``close()`` because every
test's event loop is gone by then.
"""

import gc
import threading
import time
import warnings


def release_stray_threads() -> int:
    """Stop every aiosqlite worker thread a test left running.

    Returns how many were released, so a caller (and a test) can tell "nothing
    to do" from "something survived this suite".
    """
    try:
        import aiosqlite
    except ImportError:  # pragma: no cover - the suite cannot run without it
        return 0

    gc.collect()
    released = 0
    threads = []
    for obj in gc.get_objects():
        if type(obj) is not aiosqlite.Connection:
            continue
        # `_running` is aiosqlite's own flag: True until the stop sentinel has
        # been consumed. A connection that already stopped has no thread to
        # save, and touching it would queue a second sentinel nobody reads.
        if not getattr(obj, "_running", False):
            continue
        # `stop()` queues the sentinel without awaiting anything, so it needs
        # no event loop — which is the point, because every test's loop is
        # closed by now. The worker breaks out of its own loop and exits.
        # aiosqlite's `stop()` still *asks* for a loop first, and in a loop-less
        # thread that is a `DeprecationWarning` on 3.10/3.11 (deprecation
        # threatens nothing here: the path is that it raises, which `stop()`
        # handles by queueing the sentinel with `future=None`), so the line is
        # kept out of the pytest summary rather than fixed upstream.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            obj.stop()
        released += 1
        thread = getattr(obj, "_thread", None)
        if thread is not None and thread.is_alive():
            threads.append(thread)

    # The workers exit on their own now; join them *bounded* so a wedged queue
    # can never turn a tidy exit into its own hang.
    deadline = time.monotonic() + 1.0
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return released


def stray_thread_names() -> list:
    """Non-main, non-daemon threads of the process. Diagnostic helper."""
    return [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon
    ]
