"""Exercise the real audio devices: playback callback and mic VAD."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.audio import AudioPlayer, MicrophoneCapture, rms

print("=== playback: feed a 440Hz tone, check the callback drains cleanly ===")
player = AudioPlayer(sample_rate=24000)
player.start()
frames = 24000  # 1 second
samples = (np.sin(2 * np.pi * 440 * np.arange(frames) / 24000) * 12000).astype(np.int16)
pcm = samples.tobytes()
player.play(pcm)
print(f"  queued {len(pcm)} bytes, playing={player.playing}")
drained = player.wait_idle(max_wait=10)
print(f"  drained cleanly: {drained}")
player.stop()

print("\n=== microphone: 4s of real audio through the VAD ===")
utterances = []
mic = MicrophoneCapture(
    sample_rate=16000,
    frame_ms=33.0,
    vad_threshold=0.012,
    vad_silence_frames=8,
    min_speech_ms=200,
    max_utterance_s=5.0,
    on_utterance=utterances.append,
)
mic.start()
peak = 0.0
deadline = time.monotonic() + 4.0
polls = 0
while time.monotonic() < deadline:
    mic.poll()
    peak = max(peak, mic.level)
    polls += 1
    time.sleep(0.005)
mic.stop()
print(f"  polls={polls} frames_seen={mic.stats.frames} peak_rms={peak:.4f}")
print(f"  utterances emitted: {len(utterances)}")
for u in utterances:
    print(f"    {u.duration_s:.2f}s / {len(u.pcm)} bytes / wav {len(u.as_wav())} bytes")
silence = rms(b"\x00" * 3200)
loud = rms((np.ones(1600) * 32767).astype(np.int16).tobytes())
print(f"  rms() of silence   : {silence:.6f}")
print(f"  rms() of full-scale: {loud:.6f}")
