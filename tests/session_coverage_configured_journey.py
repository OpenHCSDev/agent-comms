"""One F2 installed journey through the existing configured ACP/native owner.

The source declaration is the normal wheel installer's original proof. Capture
the actual public owner's configuration in RAM, fork once, then let the existing
saved-source journey own summary, distinct input, settlement and child cleanup.
Coverage observations below consume only its completed original journal records.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import NativeForkCreation
from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import NativeEntry
from original_owner_capture import CurrentTypedCapture
from publish_retained_summary import InstalledSource, ReviewedArtifact
from compaction_source_successor_installed_journey import run


async def qualify(stage: Path, package: Path, original_python: Path,
                  source_record: Path, wheel: Path, wheel_sha256: str):
    source = FieldCodec.decode(InstalledSource, json.loads(source_record.read_text()))
    artifact = ReviewedArtifact(wheel, wheel_sha256)
    captured = CurrentTypedCapture(
        Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'), original_python
    ).read('openhcs-architecture-memory')
    original = captured.require_current()
    donor = Path(original.require_saved_session())

    def capture_source():
        return captured.require_current(), captured.retained

    await run(stage, package, donor, capture_source=capture_source,
              probe_marker='F2_DISTINCT_AFTER_COMMIT_' + uuid4().hex,
              core_source=source, core_artifacts=(artifact,))
    receipt_file = stage / 'receipt.json'
    receipt = json.loads(receipt_file.read_text())
    assert receipt['complete'] and receipt['original_source_unchanged']
    assert receipt['native_children_closed'] and receipt['distinct_input_started_once']

    def read(db):
        (creation,) = NativeForkCreation.select(db)
        with NativeEntry.open_evidence(Path(creation.session_file)) as evidence:
            _, entries = evidence.observe()
            inherited = creation.covered_prefix(evidence, entries)
            covered = evidence.covered_prefix(entries, db)
            assert inherited <= covered
            return {'inherited_entries': len(inherited), 'covered_entries': len(covered),
                    'completed_original_creation': True,
                    'completed_original_commit': receipt['manual_commit']}

    coverage = CompactionJournal.observe_readonly(
        stage / 'wire' / 'compaction-commits.sqlite3', read, absent=None)
    assert coverage is not None
    receipt.update(coverage=coverage,
                   scope='Installed configured saved fork/compaction/distinct answer and original coverage',
                   provider_study=False, original_replays=0)
    receipt_file.write_text(json.dumps(receipt, indent=2) + '\n')
    receipt_file.chmod(0o600)
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    asyncio.run(qualify(*(Path(value).absolute() for value in sys.argv[1:6]), sys.argv[6]))
