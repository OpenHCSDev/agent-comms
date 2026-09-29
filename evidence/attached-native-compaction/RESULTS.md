# Attached runtime native compaction route

Base PR322 head63898a6e. Test-only persistent branch
`test/attached-native-compaction-20260928`. No live install or product edits.

The existing test_native_retained_child_reloads_manual_compaction now enables
RuntimeServer, creates the production CommsClient attachment, calls load_session,
checks its real RuntimeProxy socket path, and sends the canonical ACP CompactRequest.
There is no mocked runtime endpoint and no direct compact_context call. A localhost
HTTP provider controls only provider responses; the installed native CLI, socket
request handler, FieldCodec result, ACP decoder and client notifications are real.

Retained assertions: warmup/reuse same native child; summary while child alive and
history unchanged; four provider calls total; one journal/native commit; child
reaped; new native child receives summary/new input and excludes discarded history;
all children and runtime socket cleaned up. Client additionally receives exactly
one successful manual terminal, transcript invalidation and settlement, and ACP
commit identity resolves to the actual committed journal row. Attachment owns no
native backend. One existing test strengthened, no duplicate acceptance matrix.

## Latest skill/pattern review

Reread both current nra-refactoring and refactor-audit SKILL.md, pattern README,
full over-time.md, implementation.md, agent-defaults.md, boundaries.md and surface
receipt reference. Current unmerged scope classification:

- TIME-9: internal CompactRuntimeRequest/RuntimeProxy reply uses corrected ordinary
  derived family; no adapter, boolean tag, wire mirror or codec subclass added.
  External ACP/Pi are real contracts; journal format is unchanged.
- BOUND-1/BOUND-2: notifications decode once at the attached client callback through
  existing decode_updates; repeated decode-at-every-poll comprehension deleted.
- IMPL-4/IMPL-5: direct compact_context test caller/import removed; whole affected
  socket/ACP route replaces it in the same test. No parallel result dispatch or
  incomplete consumer added. Type selections in assertions observe existing facts,
  not product dispatch.
- IMPL-14: waiting tests three named public completion facts, bounded10s; no new
  anonymous domain-validation chain or duplicated reservation rules.
- AGENT-8/BOUND-5: tooling follows the same rules. Deleted embedded Node preload
  program; real tests/native_local_provider.cjs supplies only actual Pi configuration
  and localhost network guard, checked with node --check. No production test switch.
- AGENT-3/AGENT-7: no new internal golden, parallel matrix, broad rerun or CI hold.

## Evidence

Own noneditable package .venv/lib/python3.14/site-packages/agent_comms based322;
actual pinned5fde native bundle. installed-final.log: socket/native route passed
24.97s before final decode-once/static-helper cleanup. installed-pattern-current.log
is the final version's affected-route receipt. Lint, node --check, diff checks pass.
Initial installed.log preserves invalid empty prompt-list fixture rejection;
installed-current.log preserves fixture misuse of dict client notifications as SDK
objects. These were corrected via the actual current interface, no product aliases.
No paid provider calls, original replay, root changes or unchanged S7 reruns.

## Corrected newest skill source (22:16)

Verified actual skill symlink resolves to
/home/ts/.local/share/agent-comms/skills/refactor-audit-20260928-2216/refactor-audit.
Reread changed SKILL.md, pattern README, IMPL-14 final section and actual
scripts/audit/chain_terms.py. Semantic classification now uses the declared TermKind
owners, not a rule-family default: identity/snapshot IDEN-1; absence/owned state
IDEN-3; own flags IMPL-10; boundary type BOUND-1; literal cases IMPL-1; unrelated
predicates IMPL-14. This test-only change adds observation/assertion of existing
public lifecycle facts, not domain validation; no new rule family or split-rule
workaround is introduced.

Canonical BooleanChainTerms measurement of the touched Python file against322 is
recorded in chain-terms.json, including actual ChainProfile dominant-kind analysis.
No touched-file chain-term increase. The new CJS fixture is actual Node code,
syntax checked and executed, not hidden embedded Python text.

Final affected installed socket/native test passes **23.77s** in
installed-pattern-current.log after decode-once/static-helper cleanup. It preserves
four calls, one commit, fresh resumed context and child/socket cleanup. No remaining
affected-route blocker. Tests:27 deleted/82 added; static fixture14 added. Added
observations prove the previously uncovered real attachment route; no new matrix.
