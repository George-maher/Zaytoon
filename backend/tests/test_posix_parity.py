"""macOS and Linux reach the same answers, or say why they cannot.

The product decision is Windows/macOS/Linux parity (2026-10-10), and CI proves two
of those three. The third cannot be run in this sandbox, so what is verified here is
the part that is *mine*: which argv a platform chooses, how its output is parsed, and
that a missing capability degrades into a stated limitation instead of a crash or an
invented number. Every test below fakes the platform or the command output and runs
the real decision logic on Linux.

What is **not** claimed: that `open`, `osascript`, `vm_stat` or `sysctl` behave as
documented on a Mac. They are single fixed argv calls, and the CI macOS leg is where
that gets answered.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import system_resources as resources  # noqa: E402
from tools import system as system_tools  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestMacAppBundleResolution:
    def test_a_bundle_on_disk_is_found_case_and_space_insensitively(self, tmp_path):
        from tools.applications import _mac_bundle

        apps = tmp_path / "Applications"
        (apps / "Spotify.app").mkdir(parents=True)
        (apps / "Google Chrome.app").mkdir(parents=True)
        roots = [str(apps)]
        assert _mac_bundle("spotify", roots=roots) == str(apps / "Spotify.app")
        assert _mac_bundle("SPOTIFY", roots=roots) == str(apps / "Spotify.app")
        assert _mac_bundle("googlechrome", roots=roots) == str(apps / "Google Chrome.app")
        assert _mac_bundle("chrome", roots=roots) == str(apps / "Google Chrome.app")

    def test_a_missing_app_is_none_not_a_guess(self, tmp_path):
        from tools.applications import _mac_bundle

        apps = tmp_path / "Applications"
        (apps / "TextEdit.app").mkdir(parents=True)
        assert _mac_bundle("photoshop", roots=[str(apps)]) is None
        # A directory called `Fake.app` that is not a directory is not a bundle.
        (apps / "NotAnApp.app").write_text("")
        assert _mac_bundle("notanapp", roots=[str(apps)]) is None
        assert _mac_bundle("", roots=[str(apps)]) is None
        assert _mac_bundle("anything", roots=["/definitely-not-here"]) is None

    def test_no_recursive_hunt_through_other_bundles(self, tmp_path):
        """Depth one: a helper inside SomeoneElse.app is not what the user named."""
        from tools.applications import _mac_bundle

        apps = tmp_path / "Applications"
        nested = apps / "Parent.app/Contents/Helpers/Spotify.app"
        nested.mkdir(parents=True)
        assert _mac_bundle("spotify", roots=[str(apps)]) is None

    def test_open_app_uses_open_on_darwin(self, tmp_path, monkeypatch):
        from tools import applications as apps_mod

        bundle = tmp_path / "Applications" / "Spotify.app"
        bundle.mkdir(parents=True)
        monkeypatch.setattr(apps_mod, "_IS_MACOS", True)
        monkeypatch.setattr(apps_mod, "_MAC_APP_DIRS", [str(tmp_path / "Applications")])
        monkeypatch.setattr(apps_mod.shutil, "which", lambda *a, **k: None)
        assert apps_mod._resolve_posix("spotify") == ["open", str(bundle)]
        assert apps_mod._resolve_posix("nope") is None

    def test_locate_reports_the_bundle_as_positive_evidence(self, tmp_path, monkeypatch):
        from tools import applications as apps_mod

        bundle = tmp_path / "Applications" / "Code.app"
        bundle.mkdir(parents=True)
        monkeypatch.setattr(apps_mod, "_IS_MACOS", True)
        monkeypatch.setattr(apps_mod, "_MAC_APP_DIRS", [str(tmp_path / "Applications")])
        monkeypatch.setattr(apps_mod.shutil, "which", lambda *a, **k: None)
        found = apps_mod._locate("vscode")
        assert found == (str(bundle), "bundle")

    def test_linux_still_uses_the_path_and_not_the_bundle_scan(self, monkeypatch):
        from tools import applications as apps_mod

        monkeypatch.setattr(apps_mod, "_IS_MACOS", False)
        monkeypatch.setattr(
            apps_mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "spotify" else None
        )
        assert apps_mod._resolve_posix("spotify") == ["/usr/bin/spotify"]


class TestDarwinCounters:
    def test_cpu_ticks_parse_like_proc_stat(self):
        parse = resources.parse_cp_time
        assert parse("100 20 30 800 10") == (960, 160)
        assert parse("1 2 3") is None          # too few fields
        assert parse("user nice system idle") is None  # not numbers
        assert parse("") is None

    def test_cpu_percent_across_two_samples(self, monkeypatch):
        # The clock is pinned: Windows' monotonic() does not advance between
        # two back-to-back calls here (measured: delta 0.0), so `_cpu` would
        # honestly report "sampling" for the second reading forever. The
        # arithmetic under test is the delta, not the OS timer — real samples
        # are seconds apart, where the clock does advance.
        samples = iter([(1000, 100), (2000, 400)])
        clock = iter([100.0, 101.0])
        monkeypatch.setattr(resources, "_cpu_ticks", lambda: samples.__next__())
        monkeypatch.setattr(resources, "_PREV", None)
        monkeypatch.setattr(
            resources,
            "time",
            SimpleNamespace(monotonic=lambda: next(clock)),
        )
        first = resources._cpu()
        assert first == {"percent": None, "status": "sampling"}, first
        second = resources._cpu()
        assert second["status"] == "ok" and second["percent"] == 30.0, second

    def test_vm_stat_pages_become_bytes(self):
        text = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            # A 16 GB Mac uses 16 KiB pages, so the whole machine is 1048576
            # pages — the counts below are sized to fit that, and a fixture that
            # does not would hide a clamping bug behind a clamping bug.
            "Pages free:                             342134.\n"
            "Pages active:                           400000.\n"
            "Pages inactive:                         180001.\n"
            "Pages speculative:                        54099.\n"
            "Pages throttled:                               0.\n"
            "Pages wired down:                       150000.\n"
            "Pages purgeable:                          30000.\n"
            "Pages occupied by compressor:              60000.\n"
        )
        measured = resources.parse_vm_stat(text, 16384, 17179869184)
        assert measured is not None
        pages = 400000 + 150000 + 60000 - 30000
        assert measured["used_bytes"] == pages * 16384, measured
        assert measured["total_bytes"] == 17179869184
        assert measured["percent"] == round(100.0 * (pages * 16384) / 17179869184, 1)
        # Same keys as the Linux branch, so /system has one shape on every OS.
        assert set(measured) == {"percent", "used_bytes", "total_bytes"}

    def test_compressor_memory_is_counted_not_dropped(self):
        """A MacBook with 6 GB compressed must not read as 6 GB free."""
        base = {"Pages free": 1000, "Pages active": 1000, "Pages wired down": 1000}
        text = "\n".join(f"{k}: {v}." for k, v in base.items())
        lean = resources.parse_vm_stat(text, 4096, 100 * 4096 * 4096)
        fat = resources.parse_vm_stat(
            text + "\nPages occupied by compressor: 5000.", 4096, 100 * 4096 * 4096
        )
        assert fat["used_bytes"] - lean["used_bytes"] == 5000 * 4096

    def test_used_never_exceeds_total(self):
        inflated = resources.parse_vm_stat(
            "Pages active: 999999.\nPages wired down: 999999.", 16384, 1024 * 16384
        )
        assert inflated["percent"] == 100.0
        assert inflated["used_bytes"] == inflated["total_bytes"]

    def test_vm_stat_degrades_instead_of_inventing(self):
        assert resources.parse_vm_stat("", 4096, 1000) is None
        assert resources.parse_vm_stat("Pages free: 5.", 0, 1000) is None
        assert resources.parse_vm_stat("Pages free: 5.", 4096, 0) is None
        assert resources.parse_vm_stat("not a stat output", 4096, 1000) is None

    def test_unknown_platform_says_so_and_does_not_crash(self, monkeypatch):
        for system in ("Darwin", "FreeBSD", "SunOS"):
            monkeypatch.setattr(resources.platform, "system", lambda s=system: s)
            monkeypatch.setattr(resources, "_run", lambda argv: "")
            assert resources._memory()["status"] == "unavailable"
            assert system in resources._cpu()["reason"]

    def test_darwin_memory_path_uses_vm_stat_when_present(self, monkeypatch):
        canned = {
            "vm_stat": (
                "Pages free: 100.\nPages active: 200.\nPages wired down: 50.\n"
                "Pages occupied by compressor: 40.\nPages purgeable: 10.\n"
            ),
            "hw.memsize": "17179869184",
            "vm.page_size": "4096",
        }
        monkeypatch.setattr(resources.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            resources, "_run",
            lambda argv: canned[argv[2]] if argv[0] == "sysctl" else canned["vm_stat"],
        )
        measured = resources._memory()
        assert measured["status"] == "ok"
        assert measured["total_bytes"] == 17_179_869_184
        assert measured["used_bytes"] == (200 + 50 + 40 - 10) * 4096


class TestVolumeBackends:
    def test_backend_choice_probes_rather_than_assumes(self, monkeypatch):
        found = {"pactl": "/usr/bin/pactl"}
        monkeypatch.setattr(system_tools.os, "name", "posix")
        monkeypatch.setattr(system_tools.sys, "platform", "linux")
        monkeypatch.setattr(system_tools.shutil, "which", lambda n: found.get(n))
        assert system_tools._volume_backend() == "pulse"
        found.clear()
        found["amixer"] = "/usr/bin/amixer"
        assert system_tools._volume_backend() == "alsa"
        found.clear()
        assert system_tools._volume_backend() == ""
        monkeypatch.setattr(system_tools.sys, "platform", "darwin")
        assert system_tools._volume_backend() == "macos"
        assert system_tools.volume_argv("macos", level=40) == [
            "osascript", "-e", "set volume output volume 40"
        ]

    def test_mute_is_absolute_everywhere_it_can_be(self, monkeypatch):
        """The Windows path cannot be, and now says so instead of pressing a key."""
        assert system_tools.volume_argv("pulse", mute=True)[-1] == "1"
        assert system_tools.volume_argv("pulse", mute=False)[-1] == "0"
        assert system_tools.volume_argv("alsa", mute=True)[-1] == "mute"
        assert system_tools.volume_argv("alsa", mute=False)[-1] == "unmute"
        assert system_tools.volume_argv("macos", mute=False) == [
            "osascript", "-e", "set volume output muted false"
        ]
        assert system_tools.volume_argv("windows", mute=True) is None
        assert "cannot read the " in system_tools.volume_refusal("windows", mute=True)

    def test_level_is_clamped_and_never_interpolated_raw(self):
        assert system_tools.volume_argv("alsa", level=150) == ["amixer", "set", "Master", "100%"]
        assert system_tools.volume_argv("alsa", level=-5)[-1] == "0%"
        assert system_tools.volume_argv("alsa", level="42")[-1] == "42%"
        # A payload that is not a number is a refusal, not an argv entry.
        assert system_tools.volume_argv("alsa", level="42%; rm -rf /") is None
        assert system_tools.volume_argv("macos", level="0'); do-shell(") is None

    def test_windows_level_is_absolute_not_relative(self):
        """The exact bug this phase was opened for, pinned as arithmetic."""
        script = system_tools.volume_script_for(42) if hasattr(
            system_tools, "volume_script_for"
        ) else system_tools._volume_script(42, None)
        assert "175, 100" in script, "no climb to the ceiling first"
        assert "174, 58" in script, "down-keys must be 100 - level, not level"
        assert "174, 42" not in script
        assert system_tools._volume_script(100, None).count("SendKeys") == 1

    def test_parsers_read_the_real_output_shapes(self):
        pactl = "Volume: front-left: 65536 /  100% / 0x11000,\n       front-right: 65536 /  100% / 0x11000"
        assert system_tools.parse_pactl_volume(pactl) == 100
        assert system_tools.parse_pactl_volume("front-left: 100352 /  153% /") == 153
        assert system_tools.parse_pactl_volume("") is None
        assert system_tools.parse_amixer_percent(
            "Mono: Playback 65536 [100%] [0.00dB] [on]\n"
        ) == 100
        assert system_tools.parse_amixer_percent("[  42%] [off]") == 42
        assert system_tools.parse_amixer_percent("[off]") is None
        assert system_tools.parse_volume_number("67") == 67
        assert system_tools.parse_volume_number("not a level") is None
        assert system_tools.parse_volume_number("300") is None

    def test_no_backend_is_a_refusal_the_user_can_act_on(self, monkeypatch):
        monkeypatch.setattr(system_tools, "_volume_backend", lambda: "")
        tool = system_tools.GetVolume()
        result = run(tool.execute(level=20))
        assert result.success is False
        for dialect in ("Windows", "macOS", "pactl", "amixer"):
            assert dialect in result.error

    def test_a_failing_command_reports_its_stderr_not_a_lie(self, monkeypatch):
        async def fake(argv, timeout=8.0):
            return 1, "", "Connection refused"

        monkeypatch.setattr(system_tools, "_volume_backend", lambda: "pulse")
        monkeypatch.setattr(system_tools, "_run_argv", fake)
        result = run(system_tools.GetVolume().execute(level=20))
        assert result.success is False
        assert "Connection refused" in result.error
        assert "pulse" in result.error

    def test_a_successful_set_reports_the_state_it_asked_for(self, monkeypatch):
        async def fake(argv, timeout=8.0):
            return 0, "", ""

        monkeypatch.setattr(system_tools, "_volume_backend", lambda: "alsa")
        monkeypatch.setattr(system_tools, "_run_argv", fake)
        result = run(system_tools.GetVolume().execute(level=33))
        assert result.success is True
        assert result.data == {"backend": "alsa", "level": 33}
        assert "33%" in result.output

    def test_windows_read_says_what_it_cannot_know(self, monkeypatch):
        async def fake(argv, timeout=8.0):
            return 0, "Speakers (Realtek)", ""

        monkeypatch.setattr(system_tools, "_run_argv", fake)
        result = run(system_tools._read_volume("windows"))
        assert result.success is True
        assert result.data["level"] is None
        assert "percentage is unknown" in result.output

    def test_tier_still_follows_the_arguments(self):
        """Reading is safe, writing is not — unchanged by the platform work."""
        tool = system_tools.GetVolume()
        assert tool.permission_for_params({}) == "safe"
        assert tool.permission_for_params({"level": 5}) == "confirm"
        assert tool.permission_for_params({"mute": True}) == "confirm"


class TestShapeConsistencyAcrossSystems:
    def test_darwin_memory_reports_the_same_keys_as_linux(self, monkeypatch, tmp_path):
        # The Linux arm of this comparison reads /proc/meminfo, which exists only
        # on Linux. On the macOS runner that branch had nothing to read, returned
        # `unavailable`, and the assertion "Darwin and Linux report alike" silently
        # degraded into "this host is not a mac". Canning the file makes the test
        # measure the code on every OS the leg runs on.
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            "MemTotal:        16777216 kB\n"
            "MemFree:          1048576 kB\n"
            "MemAvailable:     8388608 kB\n"
            "Buffers:           524288 kB\n"
            "Cached:           4194304 kB\n",
            encoding="ascii",
        )
        monkeypatch.setattr(resources, "_LINUX_MEMINFO", str(meminfo))
        canned = {
            "vm_stat": (
                "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                "Pages free: 100.\nPages active: 200.\nPages wired down: 50.\n"
                "Pages occupied by compressor: 40.\nPages purgeable: 10.\n"
                "Pages occupied by compressor: 40.\nPages purgeable: 10.\n"
            ),
            "hw.memsize": "17179869184",
            "vm.page_size": "4096",
        }
        monkeypatch.setattr(resources.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            resources,
            "_run",
            lambda argv: canned[argv[2]] if argv[0] == "sysctl" else canned["vm_stat"],
        )
        darwin = resources._memory()
        monkeypatch.setattr(resources.platform, "system", lambda: "Linux")
        linux = resources._memory()
        assert darwin["status"] == linux["status"] == "ok"
        assert darwin["used_bytes"] == (200 + 50 + 40 - 10) * 4096
        assert set(linux) - set(darwin) == set(), (set(linux), set(darwin))
        assert darwin["total_bytes"] == 17179869184
