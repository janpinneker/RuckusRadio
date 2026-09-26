"""Mix core logic: channel conversion, summing, clipping, source lifetime.

No real devices are opened - the callbacks are called by hand with plain numpy
buffers, which is exactly what they see at runtime. This is the device-independent
core that `sinkgroup.py`'s Target/SinkGroup build on; their own mixing, mic-fanout
and mute/drift behavior is covered in test_sinkgroup_logic.py instead.
"""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

_TMP = tempfile.mkdtemp(prefix="ruckus-virtualmic-")
os.environ["RUCKUS_DATA_DIR"] = _TMP  # never touch the real %APPDATA%\Soundboard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard.virtualmic import (  # noqa: E402
    MixSource,
    mix_blocks,
    to_stereo,
)


def const(frames: int, value: float, channels: int = 2) -> np.ndarray:
    return np.full((frames, channels), value, dtype=np.float32)


def test_to_stereo():
    assert to_stereo(np.zeros(4, dtype=np.float32)).shape == (4, 2)
    mono = np.array([[0.5], [-0.5]], dtype=np.float32)
    assert np.array_equal(to_stereo(mono), np.array([[0.5, 0.5], [-0.5, -0.5]], dtype=np.float32))
    stereo = const(3, 0.2)
    assert to_stereo(stereo) is stereo
    assert to_stereo(np.zeros((3, 6), dtype=np.float32)).shape == (3, 2)
    print("to_stereo: OK")


def test_mix_blocks_sums_and_clips():
    out = mix_blocks([const(4, 0.25), const(4, 0.5)], 4)
    assert np.allclose(out, 0.75)
    # 150 % volume on two loud sounds must saturate, never wrap around
    loud = mix_blocks([const(4, 0.9), const(4, 0.9), const(4, -2.0)], 4)
    assert loud.min() >= -1.0 and loud.max() <= 1.0
    assert np.allclose(mix_blocks([const(4, 0.9), const(4, 0.9)], 4), 1.0)
    # a short block (end of a sound) only contributes to its own frames
    short = mix_blocks([const(2, 1.0)], 4)
    assert np.allclose(short[:2], 1.0) and np.allclose(short[2:], 0.0)
    assert np.allclose(mix_blocks([], 4), 0.0)
    print("mix_blocks sums + clips: OK")


def test_mix_source_gain_and_end():
    samples = const(5, 0.4)
    source = MixSource(samples, gain=0.5)
    first = source.pull(3)
    assert first.shape == (3, 2) and np.allclose(first, 0.2)
    tail = source.pull(3)  # 2 frames left -> zero padded, marks itself finished
    assert tail.shape == (3, 2)
    assert np.allclose(tail[:2], 0.2) and np.allclose(tail[2:], 0.0)
    assert source.finished.is_set()
    assert source.pull(3) is None
    print("MixSource gain, padding, end: OK")


def test_mix_source_stop():
    source = MixSource(const(100, 0.5), gain=1.0)
    source.stop()
    assert source.pull(4) is None and source.finished.is_set()
    print("MixSource stop: OK")


def main():
    test_to_stereo()
    test_mix_blocks_sums_and_clips()
    test_mix_source_gain_and_end()
    test_mix_source_stop()
    print("\nALL VIRTUAL MIC LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
