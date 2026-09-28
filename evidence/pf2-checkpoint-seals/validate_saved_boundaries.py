"""Owned-copy comparison; original roots only read, no provider/native input replay."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from agent_comms.checkpoint_seals import FinalSeal
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import (
    PrefixCertificate,
    certified_initial_page_unlocked,
    install_private_bus_checkpoint,
)
from agent_comms.wire_log import WireLog


def revision(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def main():
    roots = [
        "/home/ts/.agent-comms",
        "/var/tmp/agent-comms-live-20260927-6_d_vdul",
        "/var/tmp/agent-comms-live-20260927-wzjtqhza",
    ]
    results = []
    parent = Path(__file__).resolve().parents[2] / ".artifacts"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="saved-boundaries-", dir=parent) as directory:
        for index, source_name in enumerate(roots):
            source = Path(source_name)
            names = ["bus.jsonl", "bus_meta.json"]
            if (source / "private_bus_checkpoint.sqlite3").exists():
                names.append("private_bus_checkpoint.sqlite3")
            target = Path(directory) / str(index)
            target.mkdir(mode=0o700)
            before = {name: revision(source / name) for name in names}
            for name in names:
                shutil.copyfile(source / name, target / name)
                (target / name).chmod(0o600)
            assert before == {
                name: revision(source / name) for name in names
            }, "Source changed during capture; retry separately"
            wire = WireLog(target / "bus.jsonl")
            raw = json.loads((target / "bus_meta.json").read_text())
            marker = wire.read_metadata_unlocked()
            assert FieldCodec.encode(marker) == raw
            original = (target / "bus.jsonl").read_bytes()
            rows = original.splitlines()
            result = {
                "source": source_name,
                "bus_rows": len(rows),
                "bus_bytes": len(original),
                "marker_lossless": True,
                "private": marker.private,
                "claims": marker.claims,
            }
            if marker.checkpoint_seal is not None:
                with sqlite3.connect(target / "private_bus_checkpoint.sqlite3") as db:
                    db.row_factory = sqlite3.Row
                    row = dict(db.execute("SELECT * FROM certificate WHERE singleton=1").fetchone())
                    certificate = FieldCodec.decode(PrefixCertificate, row)
                    assert FieldCodec.encode(certificate) == row
                    assert (
                        isinstance(marker.seal, FinalSeal)
                        and marker.seal.witness == certificate.witness().seal()
                    )
                    indexed = {
                        r[0]: [
                            row[0]
                            for row in db.execute(
                                "SELECT seq FROM addressed WHERE lookup=? ORDER BY seq", (r[0],)
                            )
                        ]
                        for r in db.execute("SELECT DISTINCT lookup FROM addressed").fetchall()
                    }
                # Copied inode revisions are not proof. The copied seal MUST deny.
                try:
                    wire.full_history()
                except RelationViolationError:
                    result["copied_seal_rejected"] = True
                else:
                    raise AssertionError("Copied inode incorrectly treated as authority")
                # On this disposable clone only, retire copied authority and explicitly
                # certify every original bus byte through the existing installer.
                (target / "private_bus_checkpoint.sqlite3").unlink()
                marker.checkpoint_version = None
                marker.checkpoint_seal = None
                wire.write_metadata_unlocked(marker)
                witness = install_private_bus_checkpoint(wire)
                assert witness.digest == certificate.digest
                assert witness.through_seq == certificate.through_seq
                for lookup, sequences in indexed.items():
                    collected = []
                    with wire.locked():
                        current = wire._private_marker_unlocked()
                        while True:
                            _, page, more = certified_initial_page_unlocked(
                                wire,
                                current,
                                lookup,
                                after=collected[-1] if collected else 0,
                                limit=7,
                            )
                            collected.extend(item.message.seq for item in page)
                            if not more:
                                break
                    assert collected == sequences
                with wire.locked():
                    assert not certified_initial_page_unlocked(
                        wire, wire._private_marker_unlocked(), "f" * 32
                    )[1]
                assert (target / "bus.jsonl").read_bytes() == original
                result.update(
                    certificate_lossless=True,
                    recertified_clone_lossless=True,
                    through_seq=witness.through_seq,
                    audience_lookups=len(indexed),
                    addressed_rows=sum(map(len, indexed.values())),
                )
            assert before == {
                name: revision(source / name) for name in names
            }, "Live/source files changed independently; receipt not stable"
            result["source_revisions_unchanged"] = True
            result["bus_sha256"] = hashlib.sha256(original).hexdigest()
            results.append(result)
    print(
        json.dumps(
            {
                "ok": True,
                "roots": results,
                "provider_calls": 0,
                "live_writes": 0,
                "copies_cleaned": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
