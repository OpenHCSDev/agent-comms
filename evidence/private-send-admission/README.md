# Current312 correction — 2026-09-28

Parent review correctly rejected the relocated seven/eleven-field comparisons.
Source correction **78316454** supersedes the initial ready claim below.

- Registry identity now uses ThreadIncarnation and ProcessIdentity equality,
  including the actual current process start time. Role, admission, worktree,
  exact ActiveTurn and goal each have a named refusal rule.
- NativeRuntimeInput and PromptBinding inherit shared NativeInputRecord behavior:
  coordinator ownership is the existing OwnerGenerations value, not an invented
  thread incarnation or another owner registry. No durable schema changes.
- Extended the existing ReservationRule family through a shared RuleCheck base.
  No second dispatcher/registry. Distinct rules diagnose claim/stage/attempt,
  coordinator identity, token, already-sent admission, proven session, verdict,
  binding root/source/message and native request digest. TextDigest owns digest
  equality; typed triage/full stages own attempt matching.
- The ACP InputAttempt family was inspected; those rows describe a different
  durable ledger and cannot stand in for native coordinator rows. No fake ACP
  row or synthetic historical identity is constructed.
- Both prelaunch production callers now pass their typed stage. The prelaunch
  binding uses the same identity rules as final send; deleted its independent
  string-stage and component-comparison roster. No source proofs/fences removed.
- Reproduction of a stale ProcessIdentity with the same PID is explicitly denied.
  Per-rule tests use actual production-created SQLite reservation and binding
  rows, then test each changed authority and its named refusal, for both stages.
  These are rule/fence tests, not provider acceptance.

**Installed correction evidence:** `correction-final.log`: **122 passed in71.69s**,
including all original raw-writer tests, binding/cursor/runtime checks, shared
reservation-rule tests and all3 actual pinned native/local HTTP paths.
`correction-guards.log`: **3 passed**, including the added prohibition on identity
comparison tuples and bare-PID comparisons. `correction-ratchet.json`: zero
increases against main c338e8ab. Black/Ruff/diff checks pass. NRA full context
completed31.219s with0 emitted findings; detector inventory limit remains as below.

Earlier constructor-failure receipts are retained and closed by the final run.
Parent can review/merge the correction; no install or CI wait performed here.
OwnedTurn work was paused until this correction was published and verified.

---

# S7/C0 private native send admission closure

2026-09-28. PR312. Source checkpoint `59eea1b6` on main `c338e8ab`
(includes306/308/307). Worktree `/home/ts/wt/comms-private-send-admission-20260928`.
Parent owns merge/install; no live runtime/root/route/launcher changed.

## Ownership and deletion

- Deleted the 253-line `SelectedExecution._send_boundary` closure. The runner only
  composes the admission from current values; it no longer supplies mutable runner
  state to the isolated writer.
- `PrivateSendAdmission` owns immutable captured input facts, one-use token,
  saved-session verification, journal and exclusion. The wire → bus → registry →
  SQLite → journal ordering and the held-through-raw-write lifetime remain intact.
- `NativeSendStage` owns shared reservation/claim/binding checks. Its triage/full
  subclasses own phase behavior. Full uses the existing attempt fence authority;
  triage checks the exact deferred revision. No optional-fence dispatch remains in
  send admission; the persisted wire strings and tables are unchanged.
- `RegistryOwner` owns exact captured registry identity and admission. Cursor read
  and publication reuse this same check. `ParticipantOwner` owns the distinct
  committed participant generation check.
- Deleted `_require_registry_owner` and `_require_owner`, migrated all callers,
  and removed prompt binding's circular import of the runtime coordinator.
- No compatibility reexport, secondary registry, facade forwarding or table added.
  Existing native writer, tracked session, journal, bindings and attempt owners
  remain authoritative. Native/backend/OwnedTurn files are untouched.

Before/after class sizes measured by the packaged ratchet: SelectedExecution
1268 → 1033 (-235); CursorOwner162 → 134 (-28); NativeSourceCursor190 → 190.
Production net lines increase because the boundary now has explicit typed owners;
this is not a claim of a net code-size reduction. New modules are148/157/95 lines;
all new owner methods are below100 lines. Permanent guards enforce deletion,
no coordinator import from binding, no captured runner in the extracted owners,
and S7 method/module bounds.

## Evidence

Installed wheel built with `uv pip install --python .venv/bin/python
--reinstall-package agent-comms .`; imports/tests use `.venv/lib/python3.14/site-packages`.
No editable source-path substitution.

1. `final.log`: **118 passed in74.54s**, composed of
   `test_native_send_admission.py`, `test_native_prompt_binding.py`,
   `test_coordinated_runtime.py`, `test_selected_execution_native.py`, and the
   private-send/source-proof ownership guards. This includes real SQLite/flock/raw
   pipe exclusion and busy/cancellation/partial-write cases; it also includes
   explicit unit doubles for source/receipt state transitions.
2. **Three actual pinned native cases** in that run: ordinary selected full,
   post-cutover selected full, selected existing-file write. Native Pi child and
   deterministic loopback HTTP provider perform real get_state, admission,
   tracked input proof, tool execution, fenced publication and cursor recovery.
   No paid provider calls.
3. After rebase onto current main including Wegener308 backend result changes,
   reinstalled wheel and reran precisely those three native cases plus four
   ownership guards: `current-main-native.log`, **7 passed in11.79s**.
4. Extended the existing real pipe test to attempt a second entry into the SAME
   production admission after locks release. Both triage and full paths refuse
   reuse. Revoked owners write no bytes. Dedicated probe process observes all
   owner locks held before/after raw writes.
5. `ratchet-current-main.json`: **zero metric increases**, including per-class
   size. Black/Ruff and `git diff --check` pass for changed source/tests.

Earlier red receipts are retained: an initially mistaken type import was corrected;
then110 focused cases passed and one test still used the deleted accidental
coordinator reexport. That caller now imports its actual native owner. The final
run closes both failures. `native-and-closure.log` records the intermediate12-pass
actual-native/caller proof, not an additional production acceptance claim.

Commands:

```sh
uv pip install --python .venv/bin/python --reinstall-package agent-comms .
AC_NATIVE_COPIED_PACKAGE=/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent \
  .venv/bin/pytest -o addopts='' --basetemp=.test-final -q \
  tests/test_native_send_admission.py tests/test_native_prompt_binding.py \
  tests/test_coordinated_runtime.py tests/test_selected_execution_native.py \
  tests/guards/test_private_send_ownership.py tests/guards/test_source_proof_ownership.py
.venv/bin/agent-comms-ratchet --root src/agent_comms --base c338e8ab --head 59eea1b6
```

## NRA evidence and semantic decision

NRA skill, original S7/C0 and round2 rules reviewed. Complete production dependency
context supplied with `--context-root src/agent_comms`, one parse/analysis worker,
150-second budget, `--json --raw-findings --json-payload full`. Before30.679s,
after8.453s; both completed. Summaries preserve raw findings and timing.

The before scan's sole semantic-mirror lead incorrectly associated registry owner
fields with DeclaredFamily. Those fields are exact identity fences, not a family
catalog; they were preserved and consolidated under RegistryOwner. After scan
emits no findings. R1 mapping_read/unmodeled_record_shape/redundant_type_check
leads were not emitted. CLI output exposes no analyzed/omitted detector inventory
or scan_status, so this is not a complete detector-coverage or formal-equivalence
claim. Refactor was an explicit manual ownership patch, not an NRA-proved DSL
transaction. Real execution/negative-fence evidence supplies behavior validation.

## Remaining scope

No diagnosed blocker in this private admission slice. Parent can merge/install312.
`OwnedTurn.send_boundary` still owns ordinary goal/followup/selected-summary
permissions and remains a separate S7/C0 slice. The coordinator still has other
large methods and1383 lines; this PR does not claim whole-runtime S7 completion.
Wegener's independent backend lifecycle claim was checked in308; no overlap.

CI deferred. Own disposable environments/caches/test roots are removed after
all workers finish; committed receipts and source remain. No predecessor or live
state cleanup is part of this work.
