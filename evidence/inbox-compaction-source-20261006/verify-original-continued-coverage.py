"""Observe original saved-history coverage; never reserve, send or recover.

Run with the reviewed installed Core interpreter, without a source overlay.
The source below records the current owner and its last completed turn. It is
an observation of historical coverage, not the failed turn's lost admission,
queue custody, a manual compaction invocation or permission to resume input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import PrivateRawInput
from agent_comms.continued_private_session import verify_continued_private_session
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEvidenceRead
from agent_comms.registry_store import RegistryStore
from agent_comms.selected_source import ManualSource, SessionRevision
from agent_comms.store_files import _store_lock
from agent_comms.thread_identity import TurnId


def main() -> None:
    root = Path(sys.argv[1]).resolve(strict=True)
    name = sys.argv[2]
    registry = RegistryStore(root / "registry.json")
    dispositions = InputDispositions(root / InputDispositions.filename)
    journal = root / "compaction-commits.sqlite3"
    # Existing owners open canonical lock files with a+b. Refuse absent lock
    # resources rather than creating any public file during this observation.
    for resource in (root / "wire", registry.path, dispositions.path):
        resource.with_name(f".{resource.name}.lock").resolve(strict=True)
    journal.resolve(strict=True)
    with (
        _store_lock(root / "wire", shared=True, blocking=False),
        registry.reading(blocking=False) as document,
        dispositions.reading(blocking=False) as inputs,
    ):
        owner = document.require(name)
        owner.require_idle()
        if not owner.last_finished_turn_id:
            raise ValueError("No original completed turn for this observation")
        session = Path(owner.require_saved_session()).resolve(strict=True)
        source = ManualSource(
            owner=owner.process_identity,
            incarnation=owner.incarnation,
            turn=TurnId(owner.last_finished_turn_id),
            reserved_revision=SessionRevision.observe(str(session)).require_available(),
        )
        # No queue filtering or invented pending membership. Any unresolved
        # original input remains a refusal of this stricter historical read.
        with NativeEvidenceRead.open(session) as evidence:
            def observe(db):
                raw_ids = frozenset(row.input_id for row in PrivateRawInput.select(
                    db, where="session_file=?", parameters=(str(session),)
                ))
                verify_continued_private_session(
                    root, session, source, raw_ids, inputs,
                    journal_db=db, native_reader=evidence,
                )
                _, entries = evidence.observe()
                started = {
                    row.native_id for row in inputs.rows.values()
                    if row.matches_owner(source.incarnation) and row.has_started
                }
                checked = tuple(entry for entry in entries
                    if entry.is_message and entry.message.user
                    and entry.input_id in started)
                if len(checked) != 74:
                    raise ValueError(f"Original 74-start observation changed: {len(checked)}")
                if not source.reserved_revision.current(str(session)):
                    raise ValueError("Original history changed after verification")
                return dict(
                    result="PASS", checked_started_entries=len(checked),
                    session_file=str(session),
                    scope="current original saved-history coverage, not failed-turn admission",
                    reservation=False, input_delivery=False, recovery=False,
                )

            result = CompactionJournal.observe_readonly(journal, observe, absent=None)
            if result is None:
                raise ValueError("Original compaction journal disappeared")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
