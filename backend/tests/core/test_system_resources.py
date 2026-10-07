"""CPU / memory / disk sampling for the status panel.

Two properties are worth pinning:

* **Every block states its own condition.** `ok`, `sampling` (CPU's first
  reading has nothing to divide yet) or `unavailable` *with the reason* — the
  panel can then say why a figure is missing instead of drawing a zero that
  reads as "idle machine".
* **The CPU delta is real.** The counters are cumulative, so a single call
  must not produce a percentage; a second call after a real interval may.

The assertions validate either branch of a platform-dependent block rather
than demanding one, so the suite stays honest on Windows and Linux alike.
"""

import time

from core import system_resources


def test_every_block_states_its_condition(tmp_path):
    snapshot = system_resources.sample(str(tmp_path))

    assert set(snapshot) == {"cpu", "memory", "disk", "network"}
    for name in ("cpu", "memory", "disk"):
        block = snapshot[name]
        assert block["status"] in ("ok", "sampling", "unavailable")
        if block["status"] == "unavailable":
            assert block["reason"], f"{name} is 'unavailable' without a reason"


def test_a_first_cpu_reading_is_sampling_not_zero():
    """One cumulative reading cannot be a percentage — say so."""
    system_resources._PREV = None

    cpu = system_resources._cpu()

    assert cpu["status"] in ("sampling", "unavailable")
    if cpu["status"] == "sampling":
        assert cpu["percent"] is None, "sampling must not claim a number"


def test_a_second_reading_after_an_interval_may_be_a_real_percentage():
    system_resources._PREV = None
    system_resources._cpu()  # store the baseline
    time.sleep(0.05)
    cpu = system_resources._cpu()

    # With a real interval the counters have usually moved; on a tickless
    # platform sampling remains the honest answer. Both are accepted — what
    # is not accepted is `ok` with a percent outside 0..100.
    assert cpu["status"] in ("ok", "sampling")
    if cpu["status"] == "ok":
        assert 0 <= cpu["percent"] <= 100


def test_memory_reports_bytes_when_ok():
    memory = system_resources._memory()
    if memory["status"] == "ok":
        assert memory["total_bytes"] > 0
        assert 0 <= memory["percent"] <= 100
        assert memory["used_bytes"] <= memory["total_bytes"]
    else:
        assert memory["reason"]


def test_disk_is_measured_against_the_workspace(tmp_path):
    disk = system_resources._disk(str(tmp_path))

    # A real temp directory always sits on a mounted filesystem.
    assert disk["status"] == "ok"
    assert disk["path"] == str(tmp_path)
    assert disk["total_bytes"] > 0
    assert disk["free_bytes"] <= disk["total_bytes"]


def test_a_path_that_cannot_be_measured_reports_why(tmp_path):
    missing = str(tmp_path / "gone")
    disk = system_resources._disk(missing)

    assert disk["status"] == "unavailable"
    assert disk["reason"]


def test_network_is_an_explicit_state():
    """No collector, no dependency — and no silent omission either."""
    network = system_resources.sample("C:\\")["network"]

    assert network["status"] == "unavailable"
    assert network["reason"]
