# S7 RuntimeRequest and CliCommand implementation

## Scope and ownership

Persistent worktree: `/home/ts/wt/comms-refactor-runtime-commands-20260927`.
Branch: `codex/refactor-runtime-commands-20260927`.
PR: https://github.com/OpenHCSDev/agent-comms/pull/145 (draft).
Original implementation base: history HEAD `a2c05f2`; history PR144 is now merged.
Rebased onto main `ee774f1` (includes merged PR143/S2 and PR144/history).
Tested production commit: `b9dfd9645658a41107b03f08cfc1218435c63a43`.

Integrated acceptance: **175 passed, 2 explicitly deselected, 22.89 seconds**.
`tests-acceptance.txt` is the final run on that rebased source.
`nra-acceptance.json`: **exact_compact_global, 79 analyzed, 0 omitted,
complete=true; 0 findings on the four selected production files**. This is
scoped finding output using complete package context, not a global-clean claim.
Ruff and `git diff --check` pass.

The intermediate scan caught an error-diagnostic branch repeating two goal
fields. Error diagnostics now derive the field set/types from its declaration
and A2; the final fresh complete scan verifies that correction. The cached
partial result was not used as acceptance evidence.

Production write set:

- `src/agent_comms/runtime_requests.py`: thirteen RuntimeRequest leaves; socket
  parameters are A2 fields, shared owner binding, result framing, goal revision
  diagnostics and goal snapshot results live on their ancestors. Subscription
  streaming and prompt controller scope belong to their request declarations.
- `src/agent_comms/runtime.py`: socket boundary decodes once and invokes the
  selected request. Proxy subscription and prompt-token serialization consume
  those declarations. The action switch is removed.
- `src/agent_comms/cli_commands.py`: twenty-seven CliCommand leaves own operation
  bodies and dataclass fields. Parser flags/groups/defaults project from fields;
  enum and nested family choices project from their existing owners. A2 converts
  namespace data once, including typed tags, formats and JSON tool arguments.
- `src/agent_comms/cli.py`: family-derived parser and command invocation. Original
  stdout JSON/error handling and active-route guard remain. The command switch
  and hand-maintained parser roster are removed.

Both roots reuse A1 DeclaredFamily, A2 FieldCodec and A5 Command. No second
registry, lifecycle owner, backend wrapper, persistence schema or packaging
change. Parent's coding-tool and S2/Pi-RPC write sets are untouched.

## Behavior and evidence

- `tests/fixtures/cli-parser-contract.json` captures the pre-refactor top-level
  and all 27 subcommand help documents. The golden test verifies exact output,
  including flags, choices, ordering and required mutually exclusive groups.
- `tests/test_command_families.py` verifies typed boundary values, dynamic
  environment defaults, thirteen external runtime action payloads, old domain
  error wording, historical channel/DM/all-history CLI content and provenance,
  and declaration-only extension through the real parser/main and owner socket.
- A new CLI class becomes parseable/executable without changing a roster. A new
  RuntimeRequest class works through RuntimeProxy and the actual Unix socket
  without changing either consumer. Error framing works on that same path.
- Real socket regression covers subscription identity-before-replay, transcript
  paging, model configuration, prompt RPC errors, controller-only permission,
  cancellation, owner rename/restart, lost-result non-replay, goal edits/history,
  snapshots, revision fences and explicit retry.
- Historical channel, DM and full-history CLI still use `to_display_wire()`;
  migrated content retains source incarnation evidence. Reading does not change
  current bus bytes or grant delivery authority.

The initial broad run exposed two implementation regressions (export format
family conversion and its parser choices); both were corrected. Two additional
SDK fixture cases failed because this persistent worktree has no
`extensions/pi-mcp-client/node_modules/@earendil-works/pi-coding-agent/dist/index.js`.
Their full failure evidence is retained in `tests-contracts.txt`. Those two cases
are explicitly deselected in the focused selection; the real socket controller
permission test and other MCP relay tests are included and pass. No Node bundle
was copied, no paid provider was called, and no live process was changed.

## Reproduce locally

The reusable small test environment is
`/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`.
It supplies pytest while its .pth points at the installed runtime dependencies;
`PYTHONPATH=src` selects this worktree's source. Create `.test-artifacts` first.

```sh
PYTHONPATH=src timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -o addopts='' tests/test_command_families.py tests/test_cli.py tests/test_runtime.py tests/test_runtime_goal_edit.py tests/test_runtime_goal_snapshot.py tests/test_runtime_goal_retry_running.py tests/test_mcp_relay.py tests/test_acp_input_disposition.py tests/test_input_delivery_history.py tests/test_input_delivery_current.py tests/test_exporting.py tests/test_importing.py tests/test_restart.py tests/test_private_nk_entrypoint.py tests/test_historical_views.py -k 'not test_detached_acp_turn_uses_real_package_sdk_without_model_or_provider' --basetemp=.test-artifacts/acceptance -q
```

NRA scans select all four production files with `--context-root src`, two parse
workers, two analysis workers and a 45-second budget inside a 60-second timeout.
The cached intermediate scan is explicitly partial (43/79 detectors); fresh
context scans provide complete coverage. Authored extraction and boundary
projection implement the ownership decision in S7: no existing codemod recipe
was treated as proving synthesis of both command families and argparse field
projections. Executed behavioral tests are the behavior evidence, not a claim of
native semantic equivalence proof or a complete S7 refactor.

## Integration handoff

Parent may review/merge and install with its existing deployment procedure. There
is no data migration, replay, route change or flag to enable for this refactor.
New processes import the declaration families automatically through the normal
CLI/runtime entrypoints. Existing sockets retain their current loaded version
until the parent's normal safe refresh; no worker restart was performed here.
Reversing the code install requires no bus/registry/session restoration.
CI is deferred by owner instruction. S3/S5, subscriber fixes, native tool pipeline
and deployment remain with their existing owners.
