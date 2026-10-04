# Observed owner launch interpreter

## Concrete change

ReviewedRetainedSummaryCohort no longer stores source_interpreter. The retained launch reads each original birth-bound process argv and environment, verifies the registry incarnation/process, and supplies that observation to the existing stopped-owner handoff. The publisher preflight and restart call borrow that behavior. The existing private codec control constructs the changed cohort. No runtime/API/schema/native change.

The optional interpreter on OwnerRestartRequest/RetainedOwnerLaunch.capture remains an explicit caller selection constraint, used by original-format recovery and queued restarts. It is not the removed reviewed-cohort observation. Target launch readback still requires the target interpreter. Target source/artifact hashes, all native/checkpoint/default-route checks, fresh idle/client admission and once-only receipts remain unchanged.

This closes the duplicated observation (IDEN-5/BOUND-1) through the existing owner. Complete original audit Package/Repository source/test/tool acquisition before/after is in owner-consumers.json:316 production,365 tests,53 tools, zero omissions. Named AST relations do not prove arbitrary dynamic calls.

## Confirmation and limits

The original seven private reset/file-custody/typed-codec controls are retained by hash and not repeated. Their actual installed dependency scope is historical, not a new public cutover. One proportionate current declared-codec/original-owner observation confirmation is pending. The frozen436 operators,115 operands, old DTO and publication attempt stay unchanged and continue to use their reviewed original tool. No package installation/build/environment/native copy, owner restart, provider/input/UNKNOWN replay or public effect is authorized by this tool change.

This is a future reviewed-tool format change. There is no compatibility reader for the deleted field. It does not rewrite an already frozen review plan or substitute another operator into436.
