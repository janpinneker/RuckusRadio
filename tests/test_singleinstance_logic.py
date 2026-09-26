"""Einzelinstanz-Sperre: ein benannter Windows-Mutex, zweite Anfrage scheitert."""

import os
import sys
import tempfile
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ruckus-single-")
os.environ["RUCKUS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from soundboard import singleinstance  # noqa: E402


def test_second_acquire_fails_until_released():
    name = f"Local\\RuckusRadioTest-{uuid.uuid4()}"
    first = singleinstance.acquire(name)
    assert first is not None
    assert singleinstance.acquire(name) is None, "a second holder must be refused"
    first.release()
    again = singleinstance.acquire(name)
    assert again is not None, "after release the lock is free again"
    again.release()
    again.release()  # releasing twice is harmless
    print("second instance is refused until the first releases: OK")


def main():
    test_second_acquire_fails_until_released()
    print("\nALL SINGLEINSTANCE LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
