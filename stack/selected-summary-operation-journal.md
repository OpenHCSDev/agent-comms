# PR95 selected-summary operation reservation (Python-only, default-OFF)

Status: internal candidate only. No ACP producer calls this reservation yet; no
phase-2 Pi bytes, installed package, provider request, or production opt-in.
The selected idle-child dry-run remains non-authorizing (`routeStatus:
UNVERIFIED_NO_AUTH_RESOLUTION`). Exact Pi phase-1 `2256e2bc…` independently
cleared only its prompt-preflight readiness race; it is not integrated here.

## Durable pre-send boundary

`CompactionJournal.reserve_selected_summary()` extends the existing private
SQLite DELETE/EXTRA, per-transaction post-COMMIT directory-fsync journal. It
records a fresh 128-bit lowercase hex operation ID and bounded JSON containing
source, selected model and settings witnesses **before** any future Pi RPC
write. The partial unique index preserves the valid predecessor schema;
`BEGIN IMMEDIATE` plus a SELECT-any-status guard forbids a **new** attempt on
a session with any historical row. Historical multiple terminal rows are
preserved, never deleted or replayed; exact IDs cannot be reused. There is no
automatic per-session release API. Ambiguous stdin send, process death, timeout or
post-side-effect error stays reserved/UNKNOWN and blocks later provider input
at the existing ACP final-send gate. The journal does not decide whether a
provider was paid; no retry or automatic no-spend inference follows from a
failed transport. The future producer must pass only bounded non-secret
source/model/settings witnesses; this generic journal does not inspect nested
values for credentials or generated summaries, so that producer remains a
review gate.

`CompactionJournal.begin()` refuses a competing native commit while a selected
summary is reserved, except an intent bound to the *same* operation ID. If the
summary is UNKNOWN **or terminal-looking**, all new native commits are refused. The successful
`link_selected_summary_commit()` transition requires that reserved ID in a
same-session **committed** native intent; wrong-session, different-ID,
nonterminal or UNKNOWN attempts cannot link. A post-COMMIT directory-sync
error during bookkeeping may leave a linked row despite the caller receiving
UNKNOWN. A decline transition can likewise leave `declined-prestart` on an
unacknowledged fsync. **Every selected-summary row, including linked and
pre-start declined, remains a durable final-input blocker through restart.**
The owner must not equate a terminal row with successful caller acknowledgement,
provider acceptance, or original-input permission. A correlated, pre-side-effect
Pi decline can be recorded only for `split_turn` or explicit `unsupported`,
but cannot automatically fall back to the original input. Busy, queued, changed
source/model/settings, transport loss and post-auth/stream cancellation remain
operator-blocking; `decline_selected_summary_prestart()` never infers no spend
from silence. The independent native hard-context guard remains unchanged.

## Evidence and remaining integration

Exact `47c8e70` independent review found **NON-CLEAN P2**: both link and
verified pre-start decline could return post-COMMIT fsync UNKNOWN while a
terminal-looking row passed ACP final send in this process and after restart.
See `/dev/shm/pr95-47c8-independent-VfzSGB/REVIEW.md` (SHA256
`3f7c090cae267f326be3895caec054d36279938bebf53a3c1f6e513d94501e00`).
Exact `3ae8e1d` review found the terminal-fsync correction narrowly CLEAN,
but **NON-CLEAN P2 migration/availability**: its all-status unique index
rejects valid predecessor multiple terminal rows, denying unrelated sessions
under that wire root. See `/dev/shm/pr95-3ae8-independent-Yn2gdP/REVIEW.md`
(SHA256 `b994844da3c101c3d7eb713ffe652033449d27920ce8f2f9872944ac183b41d5`).
This successor restores the predecessor partial index, transactionally rejects
all new attempts if any selected row exists, and refuses native begin when
multiple historical rows are present. All historical rows continue to block
their exact session at the ACP gate, without globally denying other sessions.
Tests also exercise same/fresh-process fsync faults, repeated fsync denial,
two-process reservation race and non-destructive migration. Bounded provider-free
serial suites: selected journal **19 passed**; native journal, ACP send barrier
and selected dry-run **26 passed**; Black/Ruff/mypy/diff checks passed (Black
on Python 3.11 warns it cannot AST-verify configured 3.13 grammar). Fresh exact
independent review remains mandatory. This is a *safety backstop*,
not an operationally complete selected summary.

Before activating a provider path, the owner must call reserve under current
owner/turn/ingress authority, serialize the **same** exact source/model/settings
into one bounded selected-child request, and never send without durable begin.
The separately frozen Pi phase-2 candidate exact `85ef9e6` independently
failed its claimed noncooperative stream deadline/output bounds, and may not
be imported or repinned. A new separately reviewed hard selected-child
retirement/watchdog and model/auth/baseURL/extension parity are required. A
future owner-scoped, exact-ID durable recovery/positive acknowledgement must
make terminal-link/decline transition safe before **any** automatic original
input: current linked/declined rows remain blockers even after a successful
call. Native CAS, local metadata, child retirement and reopened-source evidence
must bind the same operation ID. Source/correction/settings/goal changes refuse
the handoff; no UNKNOWN input is replayed. Full provider-free ACP E2E,
wheel/source suites, operator recovery and independent exact combined review
are still required.
