# PF1 complete native tool-call ownership

Base: `c895ff0` / current main212. Branch:
`refactor/pf1-native-tool-lifecycle-20260928`.
Owned tree: `/home/ts/wt/comms-pf1-native-tool-lifecycle-20260928`.

## Implemented / deleted

`native_tool_call.py` owns per-call observation, admission, correlation, waiting,
terminal completion and cancellation. Separate nominal observation/admission
states preserve socket/event arrival independence. Existing CodingCall and
SelectedToolRequest inherit it; selected request itself is the transaction, with
no duplicate payload wrapper. Its argument projection derives from declaration
fields. Existing OwnerToolSocket owns the typed call index/authenticated
transport and actual native callback dispatch. Policy hooks retain distinct
normal multi-call and selected single-call semantics.

Deleted coding socket announced/started/finished/admitted/denied collections and
shared notification; selected approval map/event, announcement/argument mirrors,
_started/_finished, completed_call_id and approve_tool_start bypass; duplicated
socket lifecycle decisions; parse_selected_request free-function interface and
all callers. SelectedToolDenied moves to the lifecycle owner; direct consumers
migrate with no alias. Existing ledger/claim/admission policies remain their
single authority. Full caller/deletion map and reasoning: `SCOPE.md`.

Closing the owner transport now cancels/drains accepted clients before awaiting
server shutdown; current Python waits for clients in server.wait_closed. Late
handlers reject a closed owner. Failed terminal receipt cannot be retried or
converted to completion by a later error terminal. Cancellation/timeout cannot
replay a pending request. Normal rejected tools can report errors as before;
selected proof still requires successful live admission plus durable terminal.

## Production paths

- `src/agent_comms/native_tool_call.py` — actual shared lifecycle, new.
- `src/agent_comms/channel_coding_tools.py` — CodingCall terminal policy and
  migrated multi-call transport, old collections deleted.
- `src/agent_comms/selected_tool_broker.py` — selected request/call owner,
  declaration-derived argument boundary, shared socket consumers and cleanup.
- `src/agent_comms/native_pi.py` — rejection type import ONLY; parent PATH fix
  preserved. Event callbacks now execute the inherited owner implementations.

Tests migrated: test_channel_coding_tools.py, test_selected_tool_broker.py;
new test_native_tool_lifecycle.py covers both policies at their real boundaries.
No paired Toad production caller identified. No coordinated_runtime.py, activity,
assignment presentation, HistoryViews, native package, live state or launch edit.
Parent's announced activity/finish_turn changes are disjoint and should survive
normal integration. Pascal PF5 is disjoint.

## Actual acceptance: 77 distinct cases

- `final-focused.txt`: **74 passed** (45 cross-policy lifecycle +29 existing
  channel/selected broker/native fake/runner hook cases).
- `native-local.txt`: **3 passed** — actual pinned native FULL runs read/edit/
  write/bash, publishes the final reply and releases claims; actual Pi loader
  admits committed selected extension and rejects an outside copy.
- `native-transport-final.txt`: **18 passed,30 deselected** — reran those3 native
  cases plus15 targeted transport/lifecycle cases after final cleanup ordering
  and typing. The deselection avoids repeating unchanged earlier parameter cases.
- `selected-boundary-final.txt`: **15 passed** after deriving selected argument
  field projection; parser, malformed requests, durable slots, live fake native
  handshake and forged/failed terminal denial.
- `lint.txt`: changed files pass Ruff. git diff --check passes.

Retained failed evidence: lifecycle-first.txt has43pass/2fail exposing wait_closed
waiting until each admission timeout. Fixed handler cancellation ordering;
lifecycle-final.txt has45pass in0.15s. first-focused.txt contains29 initial passes.
No aborted batch is counted as passing.

Coverage includes interleaved calls, socket before announcement/start, duplicate
and mismatched events/requests, missing terminal, owner revocation, failed slot
fsync, failed terminal fsync, cancellation during waiting/admission, concurrent
requests, selected one-slot restriction, authenticated peer/token, normal four
coding tools, claimed-file mutation and release. UNKNOWN slots remain consumed.
Actual native execution used a deterministic local HTTP fixture with dummy
credentials; no external/configured provider or paid call, and no native rebuild.

Reproduction (Python is the existing local test environment):

```sh
PYTHONPATH=src timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -q -o addopts='' tests/test_native_tool_lifecycle.py tests/test_channel_coding_tools.py tests/test_selected_tool_broker.py tests/test_selected_tool_native_fake.py tests/test_selected_tool_runner_hook.py
PYTHONPATH=src AC_NATIVE_COPIED_PACKAGE=/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent PI_COMPACTION_TEST_PACKAGE=/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -q -o addopts='' tests/test_selected_execution_native.py tests/test_selected_tool_native_package.py
```

NRA before/final: full package context, selected surface,79detectors,0omissions,
exact_compact_global. Intermediate scan identified two selected field mirrors;
final projection removes both. This is authored ownership migration with local
behavioral acceptance, not a generated semantic-equivalence certificate or a
claim that zero findings proves functionality. No store/data migration required.

## Handoff

Source/affected local paths are complete. Parent owns review/serial integration,
installed acceptance and activation. No CI wait or provider rerun imposed.
No remaining PF1 implementation blocker; installed/live PF1 not claimed here.
Native package read-only; no owned package copy or large session fixture created.
Owned Python/test caches cleaned after receipt capture; evidence and source stay.
