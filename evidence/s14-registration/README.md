# S14 registration authority closure

**214 production lines deleted; 224 added across nine existing modules.**
Source checkpoint `9ff2694c`, based current main `1dbb318d` (353/354/355 merged).
Owner Boyle; parent owns installation. PR357.

## Ownership and deletion

- Registration's five live-read/turn-claim paths and compaction guard no longer
  reconstruct process, status, role, admission, turn and goal validity chains.
  They consume the existing `RegistryOwner` and canonical locked snapshot.
- `ThreadStatus`/`ActiveThreadPresence` owns active-state legality. `Thread` owns
  its process, idle/current turn and optional goal projection. `TurnId` and
  `GenerationCounter` own bounded registration identifiers and positive epochs.
- Admission claims reuse the existing reservation-rule family: identity+goal
  capability composes independently of exact active-turn capture. No new rule
  roster, generic predicate list or second registry. A captured finished turn
  and unrelated title/tag changes still permit a fresh admission claim. Full
  owner-generation claims still require complete declaration equality.
- `OwnerCompactionAttestation` is the existing boundary declaration, now decoded
  once by FieldCodec. `GoalRevision` compares the actual id/revision identity;
  absent-goal pairing is owned by the attestation. No scalar codec/wrapper or
  new storage. Every prior primitive/exact-type/bounds fence remains.
- Registry lock remains held through native mutation, with the same inherited
  descriptor and child-exit lifetime. Maintenance admission stays inside claim
  transactions. No UNKNOWN disposition, journal reset, replay, live root or pin
  changed. Existing public signatures and durable formats stay identical.
- No competing selected-dispatch, native-startup/watchdog or UI diagnostics code.
  Shared native rule applicability is consumed by the existing send rules; the
  send still requires the exact captured turn while a fresh claim does not.

All existing callers use these replaced implementations; no old implementation
or compatibility re-export remains. Six new real-store cases exercise metadata
versus exact declaration, finished captured turns, raw identity drift and stale
admission witnesses. A new active presence composes the existing capability;
registration no longer requires a parallel status case.

## Evidence

| Boundary | Result / receipt |
|---|---|
| Actual registry persistence, process incarnation, compaction guard, inherited flock and turn release | 39 passed / 3.82s, `focused-serial.log` |
| Shared admission rules + selected stop/revival/alias/busy-owner callers | 17 passed / 5.09s, one test fixture failure described below; `claim-fences.log` |
| All six new store/claim cases after fixture correction | 6 passed / 0.22s, `claim-correction.log` |
| **Noneditable installed core + actual canonical d396 native**: saved private history -> ACP prompt -> selected native summary -> durable commit -> original input delivered once; no-goal exact-turn compaction | **2 passed / 25.16s**, `installed-native.log` |
| Installed strict attestation decode including bool-as-int, floats, incomplete goal pair and wrong native-field types | 18 passed / 0.47s, `attestation-decode.log` (eight overlap the earlier registry matrix) |
| Changed-source packaged ratchet | No increases: type checks -14, long chains -10, per-file chain terms -76, foreign absence probes -19; `ratchet.json` |
| Ruff F/E9/I and whitespace | Passed, `lint.log` and `git diff --check` |

The actual native host uses the existing test infrastructure with controlled
localhost provider responses, real saved sessions, ACP/core, Node/Pi, canonical
input/summary journals and file locks. It does not call a paid provider. Loaded
registration/owner modules came from the wheel in this worktree's `.venv`, with
no source PYTHONPATH; byte equality is in `installed-imports.json`. Native package:
`/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent`.
This is installed affected-path verification; parent retains final live cutover
and live UI acceptance. No claim of a new live deployment or global S14 closure.

### Retained failed attempts

- `focused-first.log`: a patch script accidentally rewrote a local import as
  well as its module import; syntax error corrected before any tests ran.
- `focused.log`: repository default pytest options request uninstalled parallel/
  coverage plugins. Corrected invocation disables default addopts and runs serial.
- `claim-fences.log`: the new stale-turn test attempted a turn-generation mismatch
  that `Thread` already rejects at construction. Changed the fixture to a valid
  Thread with a stale **admission** witness, reaching the intended registry fence.
  No production guard was weakened to make the test pass.

## Rules and remaining scope

Read current global AGENTS, NRA and authoritative refactor-audit skills/patterns.
Applied IDEN-1/3/8, BOUND-1/2, IMPL-10/14; classified identity/state/type terms before
choosing owners. Existing packaged chain-term/foreign-absence/class-excess guards
protect recurrence. No global NRA scan or new agents under resource restriction.
CI deferred. Original S14 still has other surface owners; this closes registration
live/claim/compaction authority, not all registry document lifecycle work.

Disposable roots `.scratch` and `.venv` are owned solely by this receipt. They
are removed after child/process reference inspection; cleanup receipt follows.
