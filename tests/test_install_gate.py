"""Small client checks. Acceptance is the two actual installer rehearsals, not this suite.

Run with unittest (no native-session pytest fixtures):
python -X faulthandler -m unittest discover -s tests -p test_install_gate.py
"""
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from install_gate import InstallSurface, Machine, REPO, affected, prepare_home, rehearse


class InstallGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="install-gate-unit-", dir=Path.home() / "wt")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_surface_comes_from_install_and_distribution_declarations(self):
        paths = InstallSurface(REPO, "HEAD").paths()
        for path in ("src/agent_comms/active_route.py", "src/agent_comms/owner_cutover.py",
                     "tools/cutover/carry.py", "tools/upgrade_bus.py",
                     "stack/bin/prepare-pi-native", "hatch_build.py", "pyproject.toml",
                     "experiments/pi-context-proof/prepare-copied-pi.sh"):
            self.assertIn(path, paths)
        self.assertNotIn("docs/DECISIONS.md", paths)
        self.assertIn("src/agent_comms/active_route.py", affected(REPO, "b5b8de987^", "b5b8de987"))

    def test_read_only_wal_snapshot_and_closed_store(self):
        bus = self.root / "source"
        bus.mkdir()
        (bus / "bus.jsonl").write_text('{"committed":true}\n')
        db = sqlite3.connect(bus / "live.sqlite3")
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE records(value TEXT)")
        db.execute("INSERT INTO records VALUES ('committed')")
        db.commit()
        db.execute("INSERT INTO records VALUES ('uncommitted')")
        with sqlite3.connect(bus / "closed.sqlite3") as closed:
            closed.execute("PRAGMA journal_mode=WAL")
            closed.execute("CREATE TABLE records(value TEXT)")
            closed.commit()
        closed.close()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(bus / "socket"))
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in bus.iterdir() if path.is_file()}
        destination = self.root / "snapshot"
        Machine("local-test", str(bus)).capture(destination, self.root / "snapshot.log")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in bus.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((destination / "bus/socket").exists())
        self.assertFalse((destination / "bus/live.sqlite3-wal").exists())
        with closing(sqlite3.connect(destination / "bus/live.sqlite3")) as copy:
            self.assertEqual(copy.execute("SELECT * FROM records").fetchall(), [("committed",)])
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        with closing(sqlite3.connect(destination / "bus/closed.sqlite3")) as copy:
            self.assertEqual(copy.execute("SELECT * FROM records").fetchall(), [])

    def test_sandbox_rejects_writes(self):
        target = self.root / "must-not-exist"
        result = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all",
                                 "--die-with-parent", sys.executable, "-I", "-B", "-c",
                                 "import sys; open(sys.argv[1], 'w')", str(target)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Read-only file system", result.stderr)
        self.assertFalse(target.exists())

    def test_unreachable_desktop_and_symlinks_are_failures(self):
        with self.assertRaises(Exception):
            Machine("desktop", "/unused", ("false",)).capture(
                self.root / "unreachable", self.root / "unreachable.log")
        bus = self.root / "source"
        bus.mkdir()
        (bus / "unsafe").symlink_to(self.root)
        with self.assertRaises(Exception):
            Machine("local-test", str(bus)).capture(self.root / "linked", self.root / "linked.log")

    def test_rehearsal_requires_actual_receipt_not_just_exit_zero(self):
        target = self.root / "target"
        (target / "tools").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        home = self.root / "home"
        home.mkdir()
        bus = self.root / "bus"
        bus.mkdir()
        installer = target / "tools/upgrade_bus.py"
        installer.write_text("print('{}')\n")
        with self.assertRaisesRegex(RuntimeError, "did not return a rehearsal receipt"):
            rehearse(target, target, "unused", bus, home, Path(sys.executable),
                      self.root / "rehearse.log", self.root)
        installer.write_text("print('{\"rehearsal\":{\"unchanged\":\"unused\"}}')\n")
        with self.assertRaisesRegex(RuntimeError, "skipped the actual rehearsal"):
            rehearse(target, target, "unused", bus, home, Path(sys.executable),
                      self.root / "rehearse.log", self.root)
        installer.write_text("print('{\"rehearsal\":{\"steps\":[],\"read\":{},\"files\":{}}}')\n")
        self.assertTrue(rehearse(target, target, "unused", bus, home,
                                 Path(sys.executable), self.root / "rehearse.log", self.root)["passed"])

    def test_private_home_rebinds_route_and_preserves_live_pointer(self):
        home = self.root / "home"
        context = {"root": "/foreign/bus", "home": "/foreign/home",
                   "route": {"version": 2, "root": "/foreign/bus",
                   "wire_root_id": "a" * 32}, "runtime": "/foreign/runtime"}
        bus = self.root / "bus"
        prepare_home(home, bus, context)
        route = home / ".local/state/agent-comms/active-route.json"
        self.assertEqual(json.loads(route.read_text())["root"], str(bus))
        self.assertEqual(route.stat().st_mode & 0o777, 0o600)
        self.assertEqual((home / ".local/share/agent-comms/current").resolve(),
                         (Path.home() / ".local/share/agent-comms/current").resolve())

    def test_default_root_keeps_its_position_inside_the_synthetic_home(self):
        bus = self.root / "snapshot"
        bus.mkdir()
        (bus / "bus.jsonl").write_text("original copy\n")
        home = self.root / "home"
        selected = prepare_home(home, bus, {"root": "/source/home/.agent-comms",
                                "home": "/source/home", "route": None, "runtime": None})
        self.assertEqual(selected, home / ".agent-comms")
        self.assertEqual((selected / "bus.jsonl").read_text(), "original copy\n")
        self.assertFalse(bus.exists())


if __name__ == "__main__":
    unittest.main()
