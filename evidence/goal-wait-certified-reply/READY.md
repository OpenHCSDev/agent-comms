# GoalWait original certified reply closure — Ready

PR: https://github.com/OpenHCSDev/agent-comms/pull/462

Source checkpoint: `c53449cb6fdefac52152f79f23d66ba6ba6c9340`.
Base: `ea8aa4eaee1ce3eed9aef19cd44010b592f83b26`.
Receiving/integration owner: Mendel. Accepted #457, #461 and #428 and the
parent's frozen release cohort are unchanged. This is a source checkpoint;
no public activation or default change was performed.

## Original failure and canonical owners

The baseline private real-wire reproducer first accepted reply `0054c822ad13`
(seq 1) for the original peer. Replacing that peer with another participant
using the same name made the original wait reject that reply and a replacement
peer wait accept it. See `baseline-reproducer.json` for original birth/source
evidence. No provider was used to demonstrate this defect.

`CommittedDelivery` owns the original message and frozen addressed audience.
`GoalWait` owns the captured owner/dependency incarnations and after-sequence
boundary. Their existing stable identity joins now determine reply eligibility;
today's mutable name/alias registry does not reassign an original reply.
Existing certified reads supply source evidence; no new index, cache, cursor,
seen list, source mirror or legacy reader was introduced.

## Whole consumer closure and deletion

The 13 affected production files have **121 added / 108 deleted lines**:

- `bus_publication.py`, `goal_presentation.py`, `goal_waits.py`: original
  delivery/dependency membership; delete `GoalReplyScope` and unused copied
  review sender names.
- `goal_management.py`: original source input review, closed/terminal wait
  release, and the canonical completed-reply wait operation. Missing/unreadable
  evidence cannot establish silence or successful consumption.
- `turn_input_source.py`, `owned_turn.py`, `ordinary_admission_rules.py`,
  `owned_send_admission.py`: remove copied `direct_origins` and dependency
  messages; resolve existing original references in certified custody.
- `queued_input.py`, `input_drain.py`: remove copied queued `GoalWait` and its
  equality authority. Existing direct owner input is wait-independent; original
  owner admission and goal authority still govern handoff.
- `selected_participant.py`, `selected_turn.py`, `selected_triage.py`: existing
  selected completion families consume a matching wait after successful fenced
  full response publication or committed ignore handling. Native failure,
  cancellation, UNKNOWN and mere preparation do not consume it.

Arendt approved the admission/queued-resource and selected-participant seams;
Sch approved the existing `CommittedDelivery.direct_for` capability. No shared
builder, lifecycle operator or schema was duplicated. `selected_request.py`
has no changes. The existing retired-authority guard now covers
`GoalReplyScope` and `direct_origins` too.

## Actual installed native/ACP acceptance

`test_goal_wait_certified_reply.py::test_native` passed **11.19 seconds** with a
noneditable installed candidate wheel, Python 3.14.7, unchanged reviewed native
`native-current-593b978a717ae8f6`, existing retained owner/native fixtures and
the existing localhost HTTP provider. All 294 installed Python source files
match the production source bytes. See `installed-provenance.json` for hashes,
direct_url, native pin manifest hash and process identity witnesses.

Continuous asserted path: retained real native history → canonical tool send
from dependency → actual selected native request → queued ACP owner input while
the selected request is pending → successful original fenced reply/wait
consumption → queued owner input starts and settles once → idle → cold reopen.
There were exactly three localhost requests: history seed, original selected
reply handling and queued owner followup. The original reply produced one
outbound message and one corroborated full native input. The saved journal
prefix and historical unresolved owner input remained unchanged; that input
was absent from every provider request. A further drain did no work.

Recorded native identities `4021871/27886417` and `4021671/27886114` are both
absent by the existing `ProcessIdentity.alive()` authority after fixture
shutdown. No public owner was signalled or restarted.

This proves installed Core/native/tool/ACP handling with a controlled provider;
it does **not** claim physical Toad UI acceptance or resolution of the separately
owned empty/source-awareness cursor and Pilot timeout defects. No paid call or
public input was sent.

## Focused checks and exact baseline negative contract

Logs are retained alongside this receipt:

- `source-tests02.log`: 28 passed, 5 deselected, 9.14s.
- `source-tests03.log`: 17 passed, 1 deselected, 2.30s.
- `source-tests06.log`: 8 passed, 1 deselected, 6.33s (all six owner-followup
  goal-mode/native-success variants and two original-source controls).
- `guard.log`: 1 passed, 0.53s.
- `native04.log`: actual installed journey above, 1 passed, 11.19s.

These groups overlap and are not summed. The old negative followup expectation
failed at the exact archived ea8 base: the current public caller raises
`RequestError`. Migrated controls preserve the wrapped cause, typed failure
disposition, notification publication and original exactly-once/goal/queue
assertions. No production compatibility was added. See
`exact-baseline-negative.log`; an earlier parent-worktree comparison is not
used as the exact baseline proof.

Bounded affected-file debt receipt `ratchet-summary.json`: no positive deltas,
including per-function/per-file StringDispatch/TypeSwitch subjects and arms;
one long boolean chain and two foreign absence probes removed. Ownership
review uses IDEN-6, IDEN-5 and BOUND-2, not counts alone.

## Persistence and custody

No durable format changed. `GoalWait` storage remains unchanged. Removed
review senders and queued wait were derived in-process resources. **No store
reset/carry is required.** Goal history, source/audience evidence, native
journals and unresolved input dispositions remain authoritative and preserved.

Owned scratch:

- `/home/ts/.cache/agent-scratch/comms-goal-wait-certified-reply-20260930`:
  original reproducer, source controls, exact baseline and full bounded ratchet.
- `/home/ts/.cache/agent-scratch/gw462`: candidate wheel/environment and private
  native roots n2/n3/n4, approximately 28 MB. Original journals, SQLite input
  proofs and unresolved inputs are protected. Failed private roots were not
  replayed; each corrected run used a fresh independent fixture root.

The n2/n3 attempts exposed test callback/root-length setup defects before a
selected native request completed. The existing async provider response gate
and shorter fixture test name resolved them without production workarounds.
The disposable exact-base source extraction may be removed after its source
provenance and failure log are retained; current review wheel, active PR WT,
raw native proofs and private original inputs stay protected.

Next: parent reviews and normally merges this independent checkpoint into its
coherent release source; parent/Arendt retain all activation/cutover custody.
