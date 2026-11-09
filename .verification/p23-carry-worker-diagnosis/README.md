# Installer wait diagnosis: Core 7b4c03223

Request: comms-friction 12081; one isolated case explicitly authorized in 12099. No shared source was edited and no full carry suite or completed consumer prefix was rerun.

## Observed outcomes

- Original combined attempt: **186 passed, 1 skipped, 38 deselected, KeyboardInterrupt at 297.18s**, from `/home/ts/.cache/agent-scratch/comms-friction/p23-qualified-env-consumers-core.log`. The run had one 300-second deadline for its entire manifest. There is no 60-second per-test faulthandler dump in that log. Its sampled parent stack at `InterpreterWorker.exchange` establishes only that the parent was waiting for this request's reply; it does not establish how long this case had run or where the worker stopped.
- One new, worker-only diagnostic: `PreparedMigration.capture` completed; read-back **2.441s elapsed**, exit **0** (`diagnostic.log`, `diagnostic.exit`). It used a fresh private bus and a real installer worker. It did not reproduce the original suite context.
- One authorized exact isolated test: **1 passed, 1 warning in 16.64s, exit 0** (`isolated.log`, `isolated.exit`). Original test: `tests/test_carry.py::test_a_backup_undoes_a_real_worker_failure`. Initial read-back completed in 3.348s elapsed; the intentional migration failure, restoration/reconciliation and final read-back also completed. The final read-back took 3.362s elapsed.

The isolated result verifies this case on its own. It does **not** prove the combined run had no order-dependent worker failure, nor prove the whole P23 suite passes. The whole-manifest deadline can expire while the parent happens to be in an ordinary read-back, so that stack cannot justify calling this an installer deadlock.

## Complete source request path and its limits

`PreparedMigration.capture` (`installer/upgrade.py:1121`) inventories and pins the files, then requests `outgoing.verify`. `Stack.verify` (:728) calls ReadBackCliCommand through `cli_reply` (:653). `RuntimeStack.probe` (`installer/runtime_stack.py:175`) and `InstallCommands.probe` (`installer/commands.py:187`) select the worker and encode a ProbeRequest including the current maintenance holder. `InterpreterWorker.exchange` (:92) sends that request and waits for one reply line.

The worker's `serve` (`installer/interpreter_operation.py:506`) decodes the request; `ProbeRequest.answer` (:496) installs that request's holder before running CliCommandProbe. That probe invokes the ordinary CLI parser/root selection and ReadBackCliCommand. Its specialized execute (`cli_verbs/read_back.py:25`) only reads `RegistryStore.at(root).snapshot().threads`; it does not construct the full Comms store or perform a migration. The reply returns through FieldCodec and the answer pipe.

The parent pins are not evidence of a pin-lock cycle: `RootFile.pinned` (`storage/locked_store.py:215`) is a no-op for plain files. `SQLiteFile.pinned` (:1028) retains a read-only connection without beginning a transaction. The maintenance holder is passed per request and is not frozen into the worker's launch environment.

The former worker and verified failing-root path are unavailable after finalizers. Friction confirmed that only the parent stack was retained. Neither source inspection nor the passing isolated case attributes that former worker's state below the pipe. No cause beyond these observations is claimed.

## Diagnostics and recommendation

`worker.py` adds a normal-user launch-time faulthandler timer to the worker-only diagnostic; its empty stack log is consistent with completion before the timer fired. The exact pytest case was launched with `instrumentation/sitecustomize.py`; only a parent stack file appeared, and pytest controls its own faulthandler timer. No child-side stack was captured in that successful case, so this is not presented as a worker stack diagnosis.

**No installer source change is justified by the available evidence.** Keep the 186 completed prefix and the successful isolated case; do not replay the prefix. Account separately for carry cases after this test that the continuation skipped, and for the later continuation's actual failures. If the named wait recurs, capture that worker's actual stack while it exists, using normal-user launch diagnostics or read-only py-spy, before choosing a repair. Do not add a timeout, weaken maintenance or remove pins to make the symptom disappear.

Fresh diagnostic roots and generated stacks remain local under this artifact directory; the report, scripts and small logs are the checkpointed evidence.
