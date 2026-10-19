"""The installer's concurrent owner dispatch and pre-copy admission boundary."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from cutover.backup import UpgradeRefused
from tools.upgrade_bus import BusSnapshot, Runtime

from agent_comms.comms import Comms


class InstallerBoundaries(unittest.TestCase):
    def test_nineteen_owner_requests_are_admitted_together(self):
        barrier = threading.Barrier(19)

        class Owners(Runtime):
            def owners(self, root):
                return tuple(f"owner-{index}" for index in range(19))

            def queue_restart(self, root, name):
                barrier.wait(timeout=5)
                return sentinel.queued

        queued = Owners(Path.cwd()).queue_restarts(Path.cwd())
        self.assertEqual(queued, (sentinel.queued,) * 19)

    def test_insufficient_copy_space_refuses_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            bus = base / "bus"
            bus.mkdir(mode=0o700)
            comms = Comms(bus)
            comms.messaging.initialize_private_initial_protocol()
            (bus / "kept").write_bytes(b"retained source")
            copy = base / "copy"
            snapshot = BusSnapshot.of(bus)
            needed = snapshot.needed()
            free = needed - 1
            usage = shutil._ntuple_diskusage(needed * 2, needed + 1, free)
            with (
                patch("tools.upgrade_bus.shutil.disk_usage", return_value=usage),
                self.assertRaises(UpgradeRefused) as refused,
            ):
                snapshot.copy(copy)
            self.assertIn(f"needs {needed} bytes", str(refused.exception))
            self.assertIn(f"has {free} bytes free", str(refused.exception))
            self.assertFalse(copy.exists())
            self.assertEqual((bus / "kept").read_bytes(), b"retained source")
            print(refused.exception)


if __name__ == "__main__":
    unittest.main()
