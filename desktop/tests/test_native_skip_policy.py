"""The native-library skip policy, on hosts that do not have the libraries.

The desktop suite skips the modules that need a native library this host cannot
provide. That policy had a bug that only appears on a machine missing *both*:
the audio probe was written as

    _AUDIO_MISSING = probe("sounddevice") if not _TK_MISSING else ""

so on a host without Tk the audio probe never ran, `test_audio_pipeline.py` was
never added to `collect_ignore`, and pytest died at collection with
`OSError: PortAudio library not found`. A collection error looks like a broken
audio pipeline, which is the worst possible reading — a plain Linux container
and the Docker image are both exactly this host.

`conftest.modules_to_ignore` is the decision, extracted so it can be tested
without needing a real Tk-less machine: this test runs anywhere.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _load_conftest():
    """Import the real conftest, side effects included.

    Loading it executes the module-level probe and prints the coverage notice,
    which is acceptable: it is exactly what pytest does. The module object is
    returned rather than the helper alone so this test breaks loudly if the
    conftest stops exporting it.
    """
    spec = importlib.util.spec_from_file_location("desktop_conftest_under_test", CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestModulesToIgnore:
    def test_nothing_missing_ignores_nothing(self):
        conftest = _load_conftest()
        assert conftest.modules_to_ignore(tk_reason="", audio_reason="") == []

    def test_tk_missing_ignores_only_the_tk_modules(self):
        conftest = _load_conftest()
        ignored = conftest.modules_to_ignore(tk_reason="No module named 'tkinter'", audio_reason="")
        assert set(ignored) == set(conftest._TK_DEPENDENT)
        assert "test_audio_pipeline.py" not in ignored

    def test_audio_missing_ignores_only_the_audio_modules(self):
        conftest = _load_conftest()
        ignored = conftest.modules_to_ignore(tk_reason="", audio_reason="OSError: PortAudio library not found")
        assert set(ignored) == {"test_audio_pipeline.py", "test_telemetry.py"}

    def test_both_missing_ignores_the_union(self):
        """The regression.

        Before the fix this returned only the Tk list, so
        `test_audio_pipeline.py` was collected on a host with no PortAudio and
        the whole run failed before a single test executed.
        """
        conftest = _load_conftest()
        ignored = conftest.modules_to_ignore(
            tk_reason="No module named 'tkinter'",
            audio_reason="OSError: PortAudio library not found",
        )
        assert "test_audio_pipeline.py" in ignored, (
            "a host with no PortAudio must not collect test_audio_pipeline.py"
        )
        assert set(conftest._TK_DEPENDENT).issubset(set(ignored))
        assert set(conftest._AUDIO_DEPENDENT).issubset(set(ignored))

    def test_the_union_is_de_duplicated(self):
        """`test_telemetry.py` needs both libraries; it must appear once."""
        conftest = _load_conftest()
        ignored = conftest.modules_to_ignore(tk_reason="x", audio_reason="y")
        assert len(ignored) == len(set(ignored))
        assert ignored.count("test_telemetry.py") == 1

    def test_the_lists_only_name_real_test_modules(self):
        """A stale entry silently stops testing a module that needs no natives."""
        conftest = _load_conftest()
        here = Path(__file__).resolve().parent
        for module in conftest._TK_DEPENDENT + conftest._AUDIO_DEPENDENT:
            assert (here / module).is_file(), (
                f"{module} is listed as native-dependent but does not exist"
            )
