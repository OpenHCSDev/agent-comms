# S2 Pi RPC / turn-session refactor

## Current state

Implementation complete and published in draft PR143 on
`codex/refactor-s2-pi-rpc-20260927`. Base was main f60abbb; final integration also
includes parent PR142 / eb9e149. Parent may now integrate this branch before
extending native_pi.py FULL tools. No executable owner, live bus, stack pin,
installed runtime, or other worker's files were changed.

## Ownership and closure

- `backend.py`: `TurnSession` replaces `_stream_agent_events`; closures removed.
  Configuration, child lifetime, input admission, preflight/reopen, identity
  fencing, stats settlement and terminal publication retain their actual paths.
- `pi_rpc.py`: one cancellation-safe JSON-line reader/decoder. Ordinary,
  discovery, manual, native and selected-summary/settings RPC use it. Strict
  native mode still rejects duplicate keys, incomplete/oversize/invalid records.
  Native proof validators remain stricter downstream attestation boundaries.
  Existing `_JsonLineReader` import remains only as a compatibility alias.
- `pi_events.py`: declaration-derived event identity, open UnknownPiEvent, decoded
  fields and event-owned handling. Output/retry/abort reactions derive from the
  event classes. Native proof and exact external extension interfaces retain the
  original wire record; interpreting a record is not proof of its authority.
- `pi_commands.py`: FieldCodec-derived command shapes, response and forwarding
  behavior. MutatesSession replaces the handwritten mutation roster. Existing
  guarded stdin writers stay at the admission fences; no new executor.
- `pending_requests.py`: shared ACP/RPC correlation mechanism, including
  out-of-order named and repeated anonymous commands and cancellation.
- `turn_phase.py`: declaration-derived excursion entry/exit and watchdog
  diagnostics, including tool overlap, input-clock pause and retry capabilities.
- `turn_failure.py`: failure code, text, precedence and uncertainty belong to
  one recorded winner; replaced flags/terminal precedence switch are removed.
- `turn_inputs.py`, `turn_stats.py`, `turn_usage.py`: input forwarding and
  never-replay accounting, correlated stats epochs, provider/context ownership.
- `native_pi.py`, `manual_compaction.py`, `selected_pi_route.py`,
  `selected_pi_summary_rpc.py`: complete assigned reader migration. Native
  public failure classes, selected packaged tool, raw tracked send and proof
  boundaries are preserved. Native size errors retain NativePiUnavailable.
- ACP already has the merged S1 nominal consumer. Its interface stayed intact;
  its real consumers were exercised, not duplicated or converted back to dicts.

No edits to coordinated_runtime.py, native_prompt_send.py, coordination stores,
source/delivery recovery, historical bus reads, registry, Toad or pins. Parent
PR142 arrives solely through the main merge.

## Local evidence (CI deferred)

Logs, including failed intermediate attempts, remain in `.artifacts/s2/`.
All tests used the existing integration venv, `PYTHONPATH=src`, `-o addopts=''`,
60-second outer bounds and this worktree's own basetemp. No paid provider calls.

- Baseline and initial session relocation: 205 backend/settlement tests each.
- Final combined core/native/route/boundary selection: **338 passed, 7 optional
  fixtures skipped** (`final.txt`). Final failure ownership/phase/structural
  selection: **251 passed** (`failure-final.txt`). Counts overlap.
- ACP/compaction activity/manual bridge/selected route: **136 passed, 1 optional
  skipped** (`acp.txt`). Manual compaction/fail-closed/selected selection:
  **76 passed, 1 optional skipped** (`manual.txt`). Counts overlap.
- Actual reviewed copied CLI at the owner's `/var/tmp/...pr95...` package:
  **5 passed**. Exercises loopback-only HTTP success, 429, length stop,
  configured route/settings, and strict launch (`real-rpc-fixed.txt`). The
  transport recorder was updated for the reader's readuntil API; assertions and
  local-only fetch guard remain. No duplicate native installation.
- Actual prepared SDK/RPC PR95 handoff: **17 passed, 4 deliberately inapplicable
  synthetic/private combinations skipped** across `summary-shard.txt` (6/2),
  `decline-shard.txt` (6/2), `settings-shard.txt` (5). Covers ordinary/private
  history, summary/decline, corrections, exactly-once original admission,
  configured custom model/project settings and owners without goals.
  Earlier combined selections hit the outer bound; they are not counted green.
- **10 deterministic protocol replay scenarios match the pre-S2 runner's events
  and commands**: normal, tools, provider retry, summarization retry, compaction,
  compaction abort, interrupt, unknown/malformed, extension UI, provider failure.
  `evidence/s2/replay_protocol.py` compares main f60abbb to current code and saves
  both results. It normalizes independent native IDs and wall-clock durations.
  These are local replay fixtures, not production-provider transcript captures.
- New-case tests declare a new excursion and failure without editing dispatch;
  failure-pair precedence, formats, partial-record cancellation, strict native
  rejection, anonymous/named correlation and retired mechanisms are checked in
  `tests/test_pi_rpc_nominal.py`.
- `structure.json`: new modules and TurnSession have methods <=100 lines and
  control nesting <=5. Original unrelated backend helpers are outside that
  structural receipt. No `_stream_agent_events`, string event-kind switch or
  string phase assignment remains in the migrated runner.
- NRA final full-context scan of all changed production modules completed with
  a **full payload, zero findings** (`nra-final.json`). This analyzer version
  does not emit scan_status in a successful full payload. The first scan hit
  its deadline and remains explicitly recorded as incomplete.

## Proof limits and remaining scope

No implementation blocker remains in S2. This is a refactor, not live activation
or completion of the parent's normal FULL tools/history work. Parent owns merge
and deployment. No claim of a paid-production transcript comparison, exhaustive
provider behavior, live adoption or broad full-suite pass is made.

The initial class extraction is relocation; nominal case/phase/command/failure
ownership and removed dispatch are the actual factoring. Authored AST/source
migration was needed for async ownership/control transfer; it is not represented
as an NRA equivalence proof. Full-context NRA evidence and executable local
behavior checks are reported separately.
