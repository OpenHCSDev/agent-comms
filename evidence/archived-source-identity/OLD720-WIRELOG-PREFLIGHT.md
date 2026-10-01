# Original installed Core720 archive writer read-only preflight

Exact interpreter:
`/home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929/bin/python`.
Installed direct_url identifies Core
`72062939239b0f07707406309305a056a23f925f`; no clone, PYTHONPATH source substitution,
package installation or alternate decoder.

Both original archived roots passed their original installed boundary:

| Original archive | Original root_id | Actual preflight |
| --- | --- | --- |
| source-0njqz198 | b8ff29c8e11f47f1a59b568650e580a3 | PASS,0.0325s |
| source-a7v0vr4w | aaed93d6297f4c15bccc68c211066c7e | PASS,0.00351s |

The old `_connect(readonly=True)`/`_saved` accepted the original four-column
DeliverySources schema and witness. Its actual FinalSeal.check_final accepted
the original DB revision/witness; checkpoint_seals.file_revision (five fields,
including device) matched the original PrefixWitness's bus revision exactly.
Thus the old lock barrier required no recovery transition. Actual original
WireLog.locked(blocking=False,max_bus_bytes=8MiB) entered, and the opened descriptor
matched the original named bus lock inode. Marker equality remained exact.

WireLog was constructed directly. No Comms, Registration or registry Thread decode
ran. Original bus/registry/bus_meta/derived-DB/existing-lock bytes, sizes and source
revision tuples were identical before/after both preflights. Existing locks were
confirmed present before use; no public file was created. No public reset,
publication, owner/input/native/provider operation or claimed cutover success.

Parent owns retained_index_writer.py substitution from Comms(root) to WireLog
directly and existing stopped-batch handoff. WireLog.locked yields its original
inheritable descriptor; target require_retained_writer consumes that SAME
descriptor. The demonstrated original source reader avoids the archival registry
decode defect without adding a legacy reader/adapter or bypassing a seal.
Preimages remain required before the authorized reset, as PARENT-CARRY-INVOKE.md
specifies. These preflights do not authorize additional signals/public writes.

## Corrected proof-script mistake

First preflight compared store_files.file_revision's four-field source tuple to
the certificate's five-field tuple, so its own guard declined entering the lock.
That was not a source revision change or recovery proof. The receipt is preserved
as original evidence of this mistake; parent was immediately corrected. The
second preflight uses the original checkpoint_seals revision owner and succeeds.
No production value/codec implementation was introduced to paper over this error.

Persistent exact proof script and receipts:
`/home/ts/.cache/agent-scratch/mendel-archived-source-477-20261001/old720-archive-read-preflight.py`,
`old720-archive-read-preflight.json` (first guard error), and
`old720-archive-read-preflight02.json` (actual PASS).
The successful JSON is also retained beside this note.
