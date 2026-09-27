# Foundation / S6 implementation handoff

Owner: foundation/S6 Codex worker. Assigned branch:
`codex/refactor-foundation-s6-20260927`; persistent worktree:
`/home/ts/wt/comms-refactor-foundation-s6-20260927`.

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/125
Implementation commit: `ea13fb1` (following foundation `19323fb`, `c4c4d57`).
The final documentation-only checkpoint follows this implementation commit.

## Adoption and current state

Base is main `b1e5bfd5c39ea69c507833e8ed5efc96a7fb038b` (verified remote main
still pointed there before publication). Foundation is `19323fb`; its typed
boundary follow-up is `c4c4d57`. Both commits are ready for S1 to cherry-pick,
and `/home/ts/wt/comms-refactor-dispatch-20260927/foundation-ready.md` records
both SHAs and APIs. Do not rewrite the initial SHA after dispatch.

Stable shared APIs are `agent_comms.declared_family.DeclaredFamily` and
`agent_comms.field_codec.FieldCodec`. The first reuses the published
`metaclass_registry.AutoRegisterMeta`, with one registry per family and a
collision policy; the second derives records from dataclass init fields.
The runtime dependency is `metaclass-registry>=0.1.0`; isolated validation
resolved 0.1.4. No dependency on NRA is introduced.

S6 implements public scope, limit and format families in `exporting.py`.
The only changed Comms method is `export_wire`; related unused imports were
removed. Snapshot acquisition, lock placement and alias capture are preserved.
`scope-check.json` compares all Comms method ASTs and the five protected files
against the base. S1 files and declarations.py are byte-identical to the base.

PR95 `b74774f` is not an ancestor of this branch. Its backend reader, summary
RPC and native launcher paths were not changed or transplanted. Parent cursor
observation/history migration methods were not changed. Integrators can adopt
these commits without replacing those methods. No runtime/root edits, agent
messages, deployment, restart or merge occurred.

## Ownership decision and audit delta

The audit was at f9854ab / 0887b81 using NRA 1119ca6 and 65 modules. The actual
export baseline still had 435 lines and the same two externally recovered scope
and limit enums. The current package context contains 83 modules including the
two new shared modules; this is not a claim that the old package-wide 26 findings
all disappeared.

- Scope declarations own their fields and catalog/registry resolution. A resolved
  predicate captures aliases under the existing lock. BuiltinChannel.aggregate
  owns early aggregate rejection; the channel catalog owns saved/aggregate views.
- The limit parent owns timestamp filtering and metadata-bound validation. Full
  retention is shared by FullLimit and RecentLimit. MaxBytesLimit owns the bounded
  deque and contiguous newest-suffix behavior, including oversized-row resets.
- Format cases own rendering; common JSON encoding belongs to their parent.
  Constructor parsing, uppercase constants and iteration are compatibility
  projections derived from the family registry, not a second list of formats.
- Scope/limit headers derive from dataclass fields through A2. Stored names are
  collision-checked class projections, with golden tests protecting durable names.

This follows paper1_typing_discipline/latex_jsait/content/03_model_oopsla.tex:
structurally identical cases cannot recover distinct identity from the same
observations. Class identity and an injective family name mapping carry that
identity across extensions; consumers use the behavioral contracts. Names do
not establish equivalence of arbitrary authored method bodies.

A test-only LastMessagesLimit implements the public retention hook. Existing
export traversal, parsing and encoding accept it without edits. CLI/Toad option
selection intentionally remains a separate UI decision.

## Evidence and reproduction

All evidence is under `evidence/refactor-s6/`.

- 19 foundation tests; 59 combined foundation/export tests, including 18 complete
  before/after golden files and receipts and the new-case experiment.
- 20 CLI/bus tests, including a real CLI subprocess and private-sideband/oversized
  transition export integration. Total final focused selection: 79 passing tests.
- Wheel built, installed over the editable package in an owned isolated venv,
  imported from site-packages, and exercised with family/codec/CLI smoke checks.
- Black, Ruff, diff whitespace and focused mypy for all three owned modules pass.
- NRA 52fe8b4666a20583f0ddf8ed3b7a9e89857e4809: CLI, source operation contracts,
  registry implementation, architecture playbook and the paper were inspected.
  `scope-plan.json` was simulated and applied by NRA's revision-checked
  PatchTargetOperation. The plan replaces exactly Comms.export_wire's dispatch;
  authored class bodies were edited directly. This is an explicit semantic
  decision, not a synthesized behavior-equivalence proof. Syntax/revision replay
  and executed behavioral tests are separate evidence.
- Focused baseline: two external_enum_case_recovery findings. Final full contextual
  scan: zero findings in the three selected modules, 83 package source files.
  `nra-final-full-summary.json` retains context paths, command and raw payload hash.
  Subsequent unchanged-source `nra-context-final-cached.json` reports exact_cache,
  complete=true, 79 analyzed detectors, 0 omitted. This proves the configured
  detector coverage, not correctness of every package behavior.
- Failed/partial attempts remain documented: initial full scan hit NRA's
  internal 20s startup deadline (nra-before.json; shell ceiling was 165s); default
  contextual scan also hit NRA's internal 20s deadline;
  a changed-source loop reused only 43/79 detectors. An explicit full payload
  resolved that coverage gap. The guarded scan observed >=14.22 GiB available RAM.
- The first integration environment lacked the new dependency in its subprocess;
  `integration-tests.txt` retains that failure. The isolated installed environment
  passes (`integration-tests-isolated.txt`). A new constructor-arity test also
  caught ValueError instead of TypeError for WireExportFormat(); delegating to the
  typed parse boundary fixed it, and the final 59-test run includes that test.

Test command (after installing this branch and pytest/pytest-asyncio in an owned
venv; do not use pytest's configured parallel defaults):

```sh
TMPDIR="$PWD/.scratch" PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  -o addopts='' -o cache_dir="$PWD/.scratch/pytest-cache" \
  --basetemp="$PWD/.scratch/pytest" \
  tests/test_declared_family.py tests/test_field_codec.py \
  tests/test_exporting.py tests/test_export_families.py -q
```

Integration selection: `tests/test_cli.py`,
`tests/test_coordination_cohort.py::test_initial_sideband_never_enters_export_or_public_page_budget`,
`tests/test_envelope_bus_integration.py::test_oversize_transition_cannot_brick_a_successfully_published_root`.

## Remaining before merge

The runtime CLI/Toad constructor compatibility is tested. Mypy cannot model the
unchanged `cli.py:283` call `WireExportFormat(args.format)` as an ABC-backed factory:
`consumer-types.txt` records its two diagnostics (abstract and call-arg). No other
consumer errors remain in the focused Comms/CLI check. The new public
`WireExportFormat.parse(args.format)` is equivalent at runtime and type-checks;
changing that one CLI call is the small remaining integration action. CLI edits
were outside this worker's exporter/tests plus Comms.export_wire source boundary.
Keep the PR draft until this is resolved; do not describe package-wide mypy as
passing. Toad can retain its runtime constructor or adopt parse in its own scope.

No broad suites, external model/provider calls, extra agents, CI wait, live GUI
validation, deployment, restart or merge. Disposable caches, wheel, venv and test
roots are removed at completion; retained fixtures/evidence are intentional.
