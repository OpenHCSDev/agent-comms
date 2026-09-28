# Exact successful PR165 full-context invocation

Run from `/home/ts/wt/comms-refactor-s3-deletion-20260928`:

```sh
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python \
  -m nominal_refactor_advisor \
  src/agent_comms/coordination.py src/agent_comms/coordination_store.py \
  src/agent_comms/coordination_cohort.py src/agent_comms/coordination_response.py \
  src/agent_comms/coordinated_runtime.py src/agent_comms/recovery_projection.py \
  src/agent_comms/claim_states.py src/agent_comms/execution_states.py \
  src/agent_comms/attempt_states.py src/agent_comms/obligation_states.py \
  --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 \
  --no-cache --scan-budget-seconds 140 --json --json-payload loop
```

Result: `exact_compact_global`,79 analyzed detectors,0 omissions,complete,0 reported findings,23s actual. The140s internal budget matters: setting only the shell timeout left the first invocation at NRA's default20s deadline. Cached scans can reuse only43 of79 detectors after changes; `--no-cache` removes that ambiguity. This is architecture evidence for the selected files, not a native-equivalence proof or a finding count for every file in the package.

Current relationship/passive invocation is also preserved verbatim in `nra-command.sh`.
