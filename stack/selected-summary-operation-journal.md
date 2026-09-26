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
write. One reserved/UNKNOWN attempt per saved session is indexed; exact IDs
can never be reused. Ambiguous stdin send, process death, timeout or
post-side-effect error stays reserved/UNKNOWN and blocks later provider input
at the existing ACP final-send gate. The journal does not decide whether a
provider was paid; no retry or automatic no-spend inference follows from a
failed transport. The future producer must pass only bounded non-secret
source/model/settings witnesses; this generic journal does not inspect nested
values for credentials or generated summaries, so that producer remains a
review gate.

`CompactionJournal.begin()` refuses a competing native commit while a selected
summary is reserved, except an intent bound to the *same* operation ID. If the
summary is UNKNOWN, **all** new native commits are refused. The successful
`link_selected_summary_commit()` transition requires that reserved ID in a
same-session **committed** native intent; wrong-session, different-ID,
nonterminal or UNKNOWN attempts cannot link. A post-COMMIT directory-sync
error during bookkeeping may leave a linked row despite the caller receiving
UNKNOWN; the owner still must block and inspect both exact IDs and source.
No journal status is itself owner authority or permission to replay input.

A correlated, pre-side-effect Pi decline may clear reservation only for
`split_turn` or explicit `unsupported`, after the future owner separately
rechecks original input and source. Busy, queued, changed source/model/settings,
transport loss and post-auth/stream cancellation remain operator-blocking;
`decline_selected_summary_prestart()` does not infer no spend from silence.
The independent native hard-context guard remains unchanged.

## Evidence and remaining integration

Serial provider-free suites: `tests/test_selected_summary_journal.py` **15
passed**, `tests/test_compaction_journal.py` plus
`tests/test_compaction_send_admission.py` **17 passed**. Tests include process
exit after durable reserve, duplicate ID and session exclusion, post-COMMIT
fsync uncertainty, wrong committed-intent binding, unrelated native begin
refusal, exact same-ID native begin, narrow clean decline, and original input
send exclusion. Black/Ruff/mypy and diff checks pass. One initial combined run
was interrupted after 31 dots; it grants no clearance.

Before activating a provider path, the owner must call reserve under current
owner/turn/ingress authority, serialize the **same** exact source/model/settings
into one bounded selected-child request, and never send without durable begin.
The still-unimplemented Pi phase-2 operation must attest the selected process's
actual stream/auth/extension route, return validated native compaction result
and retain no-retry/UNKNOWN semantics. Then retire the idle child, perform
native CAS with the same operation ID in its durable intent, link exact committed
ID, project only local metadata and strictly reopen before **one** original
input send. Source/correction/settings/goal changes refuse the handoff; no
UNKNOWN input is replayed. Full provider-free ACP E2E, wheel/source suites,
operator recovery and independent exact combined review are still required.
