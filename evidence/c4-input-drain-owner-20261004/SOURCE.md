# InputDrain C4 ownership work

Base current main c159f39e. The original Package parses production and tests
with omissions recorded in before.json. Dynamic callback resolution is not
proved. InputDispositions remains durable receipt authority, QueuedInput owns
live acceptance/identity, TurnInputSource owns captured source keys and goal
permission, QueueProjection owns external presentation, and InputDrain owns
loop tasks/inbox lifetimes.

Concrete duplicate: turn_input_keys copies OriginalTurnInput.keys and accepted
following-source keys. Acceptance/OwnedTurn/refusal/retirement must update that
set independently; delivery notices and terminal checks then read the mirror.
Derive those reads from original source owners instead and remove all writes.

Queue size/encoding admissibility currently lives in InputDrain instead of
its existing QueueProjection declaration. Three identical empty ACP chunks
wrap those declared updates independently. Move those real behaviors to the
existing projection/update owners, preserving external wire shapes and
actual captured queue revisions. This is an owner migration, not a new mixin
or method relocation to meet a line threshold.

Live pending queue membership and retained accepted source evidence have
different lifetimes after InputStarted/clear. Do not collapse that distinction
or reconstruct a queue from durable UNKNOWN. Current maps still require
semantic reading before any additional migration. All accepted #633/#638/
#640 evidence and original UNKNOWN remain untouched.

Source-only checkpoint: no package/provider/native/test execution. Parent
owns HistoryViews/presentation; W1 #627 and #653 source scopes are separate.

## Published production batch d68fa58f

55 production lines deleted, 59 added across the five existing owners; no new
class, store, worker, codec or schema. The original Package parses all 316
production and 365 test modules with zero omissions. after.json includes
class declarations, inheritance and consumer references; dynamic dispatch is
still source evidence rather than an execution proof. No turn_input_keys
production or test consumer remains.

- InputDrain.input_keys derives the union of OriginalTurnInput.keys and every
  retained AcceptedFollowingInput.keys. OwnedTurn no longer writes a second
  membership set; acceptance/refusal/retirement no longer maintain it.
- TurnProgress.done asks the durable InputDocument once about that union.
  Its second pending-followup test asked the same document about a subset;
  admission's pending-followup count remains because it enforces a different
  original limit.
- QueueProjection.capture owns queue size/UTF-8 admissibility; QueuedInput
  supplies only its own original ID/text under the captured admission. Rejected
  projections do not alter accepted inputs or their durable disposition.
- AgentCommsUpdate supplies the empty ACP chunk for the three original input
  publications. RuntimeServer still owns delivery/attachment and revisions
  remain at the original loop owner. The external envelope is unchanged.
- The socket notice fixture uses QueuedInput.capture and its original source
  instead of assigning the removed key mirror. The existing queue contract
  now checks clear/refusal source lifetime against unchanged durable bytes.

Following sources intentionally outlive pending queue entries. InputStarted
removes the pending entry; clear removes pending follow-ups; neither creates
proof or erases the retained source used by native admission and terminal
checks. Refusal retires its following source, turn retirement burns remaining
live grants, and durable UNKNOWN stays a notice. If an original and follow-up
share a key, refusing one cannot delete the other's original membership.

The original GodClass still measures InputDrain at 549 lines, down from 571.
This batch closes the competing key authority and projection/publication
ownership; it does not claim the full C4 size/workflow is closed. Unique
observer, native inbox and cancellation lifetimes remain in InputDrain. No
methods were carved into a mixin to meet the threshold.

Source diff whitespace check passed. No runtime test, package operation,
native input, external provider or public mutation has run for this batch.
Affected installed queue clear/refusal, retained Started source, UNKNOWN
notice and joined retirement checks await a fresh named holder purpose.
Parent owns HistoryViews/presentation and ReadyDrainReadiness's value
contract; no activity.py write is claimed here. W1 #627 metadata records are
untouched.

## Original owner-inbox retirement c265eedd

All five actual command producers are recorded in after.json: InputDrain's
clear/prompt/promote, SteerPromptRequest's control, and ConfigOptions'
SettingCommand.to_rpc. They all supply dictionaries. Selected/ordinary owner
inbox declarations and the selected consumer now share that actual contract.
The unused raw-string-to-ScheduledTurn retirement decision is deleted rather
than treating an untracked command as permission to launch another turn.
Routed ScheduledTurn work remains at its original producer; the independent
direct backend API still supports strings. Clearing pending command resources
and retiring live receipts have the same joined lifetime as before.

Final production determining head c265eedd: 64 production lines deleted,
66 added across six existing modules. Original Package remains 316 production
and 365 tests, zero omissions. InputDrain is now 547 lines. This remains a
source ownership checkpoint, not full C4 or installed readiness.

Exact evidence head da759b77 passed the automatic required Debt ratchet
(run 37220548943); that is the previous source scope. The next source head's
required job and changed installed acceptance are reported independently.
Fresh 540 purpose requested from Bohr; separate immutable086 read/execution
requested from its original Sch owner. No prior loan is reused implicitly.
