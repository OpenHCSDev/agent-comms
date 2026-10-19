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
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from install_gate import (
    DEFAULT_SCRATCH,
    REPO,
    ExistingStatusChecks,
    FieldCodec,
    GateError,
    InstallSurface,
    Machine,
    MalformedRecord,
    MissingProtection,
    NoStatusChecks,
    NumberedAppIdentity,
    ProtectionAnswer,
    PullRequestAnswer,
    RequiredCheck,
    RouteDocument,
    SnapshotCopy,
    affected,
    prepare_home,
    rehearse,
)


class InstallGateTests(unittest.TestCase):
    def setUp(self):
        DEFAULT_SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="unit-", dir=DEFAULT_SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_surface_comes_from_install_and_distribution_declarations(self):
        paths = InstallSurface(REPO, "HEAD").paths()
        for path in (
            "src/agent_comms/active_route.py",
            "src/agent_comms/owner_cutover.py",
            "tools/cutover/carry.py",
            "tools/upgrade_bus.py",
            "stack/bin/prepare-pi-native",
            "hatch_build.py",
            "pyproject.toml",
            "experiments/pi-context-proof/prepare-copied-pi.sh",
        ):
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
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bus.iterdir()
            if path.is_file()
        }
        destination = self.root / "snapshot"
        Machine("local-test", str(bus)).capture(destination, self.root / "snapshot.log")
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bus.iterdir()
            if path.is_file()
        }
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
        result = subprocess.run(
            [
                "bwrap",
                "--ro-bind",
                "/",
                "/",
                "--unshare-all",
                "--die-with-parent",
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; open(sys.argv[1], 'w')",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Read-only file system", result.stderr)
        self.assertFalse(target.exists())

    def test_unreachable_desktop_and_symlinks_are_failures(self):
        with self.assertRaises(GateError):
            Machine("desktop", "/unused", ("false",)).capture(
                self.root / "unreachable", self.root / "unreachable.log"
            )
        bus = self.root / "source"
        bus.mkdir()
        (bus / "unsafe").symlink_to(self.root)
        with self.assertRaises(GateError):
            Machine("local-test", str(bus)).capture(self.root / "linked", self.root / "linked.log")

    def test_rehearsal_requires_completed_result_not_just_exit_zero(self):
        target = self.root / "target"
        (target / "tools").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        home = self.root / "home"
        home.mkdir()
        bus = self.root / "bus"
        bus.mkdir()
        installer = target / "tools/upgrade_bus.py"
        installer.write_text("print('{}')\n")
        with self.assertRaisesRegex(GateError, "did not return a completed rehearsal"):
            rehearse(
                target,
                target,
                "unused",
                bus,
                home,
                Path(sys.executable),
                self.root / "rehearse.log",
                self.root,
            )
        installer.write_text('print(\'{"rehearsal":{"unchanged":"unused"}}\')\n')
        with self.assertRaisesRegex(GateError, "did not return a completed rehearsal"):
            rehearse(
                target,
                target,
                "unused",
                bus,
                home,
                Path(sys.executable),
                self.root / "rehearse.log",
                self.root,
            )
        installer.write_text(
            "import sys, time\n"
            "if '--help' not in sys.argv: time.sleep(2.1)\n"
            'print(\'{"rehearsal":{"steps":[],"read":{"threads":1,"who":2,"history":3},"files":{"identical":4,"rewritten":5}}}\')\n'
        )
        result = rehearse(
            target,
            target,
            "unused",
            bus,
            home,
            Path(sys.executable),
            self.root / "rehearse.log",
            self.root,
        )
        self.assertEqual(result.read.history, 3)
        self.assertEqual(result.files.identical, 4)

    def test_private_home_rebinds_route_and_preserves_live_pointer(self):
        home = self.root / "home"
        context = SnapshotCopy("/foreign/bus", "/foreign/home")
        bus = self.root / "bus"
        (self.root / "active-route.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "root": context.root,
                    "wire_root_id": "a" * 32,
                }
            )
        )
        prepare_home(home, bus, context)
        route = home / ".local/state/agent-comms/active-route.json"
        self.assertEqual(
            FieldCodec.decode(RouteDocument, FieldCodec.parse(route.read_text())).selection.root,
            bus,
        )
        self.assertEqual(route.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (home / ".local/share/agent-comms/current").resolve(),
            (Path.home() / ".local/share/agent-comms/current").resolve(),
        )

    def test_default_root_keeps_its_position_inside_the_synthetic_home(self):
        bus = self.root / "snapshot"
        bus.mkdir()
        (bus / "bus.jsonl").write_text("original copy\n")
        home = self.root / "home"
        selected = prepare_home(
            home, bus, SnapshotCopy("/source/home/.agent-comms", "/source/home")
        )
        self.assertEqual(selected, home / ".agent-comms")
        self.assertEqual((selected / "bus.jsonl").read_text(), "original copy\n")
        self.assertFalse(bus.exists())

    def test_external_answers_are_decoded_once_and_refused_if_malformed(self):
        answer = FieldCodec.decode(
            PullRequestAnswer,
            FieldCodec.parse(
                '{"headRefOid":"head","baseRefOid":"base","isCrossRepository":false,"extra":1}'
            ),
        )
        self.assertEqual((answer.head, answer.base), ("head", "base"))
        answer.require_local()
        with self.assertRaises(MalformedRecord):
            FieldCodec.decode(
                PullRequestAnswer,
                FieldCodec.parse(
                    '{"headRefOid":"head","baseRefOid":"base","isCrossRepository":"false"}'
                ),
            )
        with self.assertRaises(MalformedRecord):
            FieldCodec.decode(MissingProtection, FieldCodec.parse('{"status":"403"}'))

    def test_protection_keeps_existing_app_constraints_and_null_has_owned_behavior(self):
        answer = FieldCodec.decode(
            ProtectionAnswer,
            FieldCodec.parse(
                '{"required_status_checks":{"strict":false,"contexts":["other"],'
                '"checks":[{"context":"other","app_id":42}]},"unrelated":true}'
            ),
        )
        expected = ExistingStatusChecks(False, (RequiredCheck("other", NumberedAppIdentity(42)),))
        self.assertEqual(answer.required_status_checks, expected)
        with patch("install_gate.api_payload") as publish:
            answer.required_status_checks.require("owner/repo")
            sent = publish.call_args.args[2]
            self.assertEqual(sent.strict, False)
            self.assertEqual(sent.checks[0], expected.checks[0])
            self.assertEqual(sent.checks[1].context, "install-rehearsal")
        missing = FieldCodec.decode(
            ProtectionAnswer, FieldCodec.parse('{"required_status_checks":null}')
        )
        self.assertIsInstance(missing.required_status_checks, NoStatusChecks)
        with patch("install_gate.api_payload") as publish:
            missing.required_status_checks.require("owner/repo")
            self.assertTrue(publish.call_args.args[2].strict)


if __name__ == "__main__":
    unittest.main()
