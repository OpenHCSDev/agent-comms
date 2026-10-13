"""One-time terminal-claim release. Back up first; never replay an UNKNOWN append."""

import argparse
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.coordination_response import _response_boundary
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ClaimRelease, project_verified_transitions
from agent_comms.messages import InfoKind, Message
from agent_comms.store_files import StoreLockContention


def run(root: Path, backup: Path) -> None:
    comms = Comms(root)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with _response_boundary(comms.bus, contention=StoreLockContention(2.0),
                                session=store.session) as registry:
            with store.session.read():
                bus, lock, _ = store.session._settlement_boundary
                marker = bus.log._private_marker_unlocked()
                rows, _ = lock.claim_transitions(bus.log, marker)
                before = project_verified_transitions(rows)
                selected = []
                for claim in before.values():
                    source = claim.admission
                    if source is None:
                        continue
                    attempt = AttemptRecord.one(store.session._connection,
                                                execution_id=source.execution_id,
                                                attempt_ordinal=source.attempt_ordinal)
                    if (attempt is not None and attempt.lifecycle.terminal
                            and attempt.owner_lookup == source.recipient_lookup
                            and attempt.owner_generation == source.participant_generation):
                        selected.append(claim)

                # Creation is no-replace: this operation cannot be silently rerun.
                backup.mkdir(mode=0o700, parents=True, exist_ok=False)
                for name in ("bus.jsonl", "bus_meta.json", "private_bus_checkpoint.sqlite3",
                             "registry.json", ".registry-owner-guard", "catalog.json",
                             "input_dispositions.json", "owner_release_receipts.json"):
                    original = root / name
                    if original.exists():
                        saved = backup / name
                        shutil.copyfile(original, saved)
                        saved.chmod(0o600)
                        with saved.open("rb") as stream:
                            os.fsync(stream.fileno())
                saved_sql = backup / "coordination.sqlite3"
                with sqlite3.connect(saved_sql) as copy:
                    store.session._connection.backup(copy)
                saved_sql.chmod(0o600)
                with saved_sql.open("rb") as stream:
                    os.fsync(stream.fileno())
                with (backup / "selected.json").open("x") as record:
                    json.dump([{"resource": c.resource, "owner": c.owner,
                                "incarnation": c.incarnation, "generation": c.generation,
                                "execution": c.admission.execution_id,
                                "attempt": c.admission.attempt_ordinal} for c in selected], record)
                    record.flush()
                    os.fsync(record.fileno())
                directory = os.open(backup, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

                groups = defaultdict(list)
                for claim in selected:
                    groups[(claim.owner, claim.incarnation)].append(claim)
                counts = defaultdict(int)
                for (name, incarnation), claims in groups.items():
                    owner = registry.threads[registry.canonical_name(name)]
                    for offset in range(0, len(claims), 32):
                        batch = claims[offset:offset + 32]
                        bus.publisher.publish_claim_envelope(
                            Message(owner.name, "#all", "Terminal attempt file claims released",
                                    InfoKind, notice=True),
                            # Historical claims may predate a project move.
                            # This is release-only; the recorded generation and
                            # incarnation fence it, not today's project directory.
                            worktree=Path(Path(batch[0].resource).anchor), incarnation=incarnation,
                            releases=tuple(ClaimRelease(c.resource, c.generation) for c in batch),
                            _locked_registry_snapshot=registry, _bus_lock=lock,
                        )
                        counts[name] += len(batch)
                rows, _ = lock.claim_transitions(bus.log, bus.log._private_marker_unlocked())
                after = project_verified_transitions(rows)
                removed = {c.resource for c in selected}
                assert dict(after) == {path: c for path, c in before.items() if path not in removed}
                result = {"root": str(root), "backup": str(backup), "released": len(selected),
                          "by_owner": dict(counts), "remaining": len(after)}
                with (backup / "result.json").open("x") as record:
                    json.dump(result, record)
                    record.flush()
                    os.fsync(record.fileno())
                print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    run(args.root.absolute(), args.backup.absolute())
