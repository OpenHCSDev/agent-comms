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
