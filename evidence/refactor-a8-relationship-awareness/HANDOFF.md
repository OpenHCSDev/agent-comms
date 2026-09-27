# A8 relationship and passive-awareness adoption

Owner: Codex session `01a0e4f3-fd49-7381-8e9f-86627340b30c`, resumed in independent service PID 1910618. Worktree `/home/ts/wt/comms-refactor-a8-relationship-awareness-20260927`; branch `codex/refactor-a8-relationship-awareness-20260927`. Implementation and validation complete; publication receipt follows in `publication.json`. Parent owns integration/deployment. No blocker remains in this assigned surface.

Started from completed S8 `a6f9e5414e092b64dd6082946a485c4fc1dc806b` / draft PR134. Draft base is `codex/refactor-s8-goals-20260927`. PR134 already includes A8/PR129, A1/A2 foundation/PR125, S1/PR127 and its main merges. Keep those dependencies once; this change is not an independent replacement for them. No edits to S4's field_codec/channels/read/routing, parent registry restoration/runtime/native/coordination, PR95, ACP, Toad, or the live root. No owner restart, uncertain input replay, model override, paid provider, extra worker, deployment, merge or CI wait.

## Completed ownership

`ThreadRelationships` holds `RelationshipStore`, an existing LockedStore subclass. All collaboration/order document reads and changes use inherited shared-read/exclusive-update transactions. The store owns its filename, version boundary, missing value, and legacy indented/newline format. Collaboration uses its existing declaration through FieldCodec; RelationshipOrder declares the persisted sort row. Alias/incarnation checks, historical notes, mutual projections, goal contacts and bounded wire history remain with ThreadRelationships. Its RLock and wire/bus locks protect different authorities and remain; no relationship document locking or file algorithm remains there. Goal-only projection no longer takes an unrelated document lock.

`PassiveChannelAwareness` holds `PassiveAwarenessStore`. `PassiveAwarenessRecord` declares typed row fields, source triples and scope invariants; FieldCodec derives field validation/serialization. Scope-cutoff behavior has one record method. The versioned extensible document/row envelopes remain mappings so unknown legacy fields survive unrelated updates; typed records are temporary boundary projections, not writable caches or a second persistent authority. Missing documents return an empty envelope. Invalid/unreadable passive documents return None and remain untouched; relationship corruption still raises. Public constructors, paths and behavior methods remain compatible. Tests injecting the deleted local writer now inject the inherited store publication boundary.

Passive initialization, scope changes, frame selection and exact-witness publication are complete exclusive transactions. Read-only source capture/recheck holds a shared lock, including through exact bus validation. Repeated frames do not advance the advisory cursor or imply a native receipt. All callbacks build replacement mappings without mutating the original. Publication failure suppresses the frame; an index-close failure also suppresses any composed but unpublished frame. The bounded bus/index algorithms stay with their actual projection owner.

The only shared extension is to existing LockedStore: `reading()` keeps a shared lock through dependent verification; `read()` uses it. `_unreadable` defaults to raising and lets the optional owner declare None only for document ingress failures, never callback/publication failures. Class declarations select JSON indentation/key sorting/trailing newline. Existing atomic staging, file fsync, mode preservation, atomic replace, parent fsync and rollback remain unchanged. No alternate store, serializer, registry or metadata catalog was created.

## Validation and limits

Existing Python environment used read-only: `/home/ts/.agent-comms/.venv/bin/python`, with `PYTHONPATH=src:/home/ts/code/projects/metaclass-registry/src` and `PYTHONDONTWRITEBYTECODE=1`. Every pytest shard was externally bounded by 60 seconds, default xdist/coverage disabled, and used an owned basetemp plus `TMPDIR=/tmp/ac-a8-ra-20260927`. No network model invocation occurred.

- `final-tests.txt`: **87 passed**, dedicated relationships/passive/adopter/LockedStore cases. Covers byte-format goldens, no-op inode preservation, malformed/invalid-UTF8/unreadable storage, unknown extension preservation, real subprocess shared readers and concurrent writers, held source-recheck lock, inherited ABC algorithms, file-sync/replace/directory-sync rollback and optional frame suppression.
- `consumer-tests.txt`: **47 passed**, goal mentions, envelope bus integration and tools.
- `shared-regression.txt`: **40 passed**, existing FieldCodec, goal history and standby users.
- Ruff, Black (Python 3.11 target), scoped mypy for all three source modules and `git diff --check` pass.

174 passing cases across the three disjoint final shards. Original baseline: 32 dedicated cases pass. Initial 17 baseline failures were all AF_UNIX path-too-long errors from the long worktree TMPDIR; the short owned socket root fixed the harness. The first new golden fixture used `created` instead of the actual existing `created_at` sort value; the fixture was corrected. Failed/intermediate logs are retained.

Exact test invocation pattern:

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:/home/ts/code/projects/metaclass-registry/src \
TMPDIR=/tmp/ac-a8-ra-20260927 timeout 60 /home/ts/.agent-comms/.venv/bin/python \
-m pytest -o addopts='' -p no:cacheprovider --basetemp=$PWD/.artifacts/<shard> <tests> -q
```

No full-suite, Windows, installed-wheel, live service, mounted Toad or deployment claim. CI is deferred by owner instruction. Parent must combine this stacked change with S4 and any newer main additions through ordinary integration, preserving those owners' changes.

## NRA evidence

Applied `/home/ts/.codex/skills/nra-refactoring/SKILL.md`. API authority: NRA checkout `52fe8b4666a20583f0ddf8ed3b7a9e89857e4809`; actual CLI help, API/playbook and registered PatchTargetOperation constructor/proof scope were inspected. Baseline and final scans cover the complete `src/agent_comms` package while reporting the three owned modules. Both completed **79/79 detectors, zero omissions, zero reported findings**, `exact_compact_global`, no cache. This is no claim of zero findings across unrelated modules.

`relationships-plan.json` and `passive-plan.json` retain exact declaration-selected transaction migrations, their simulation and revision-checked application receipts, plus plan builders. The semantic choice was authored: the DSL does not automatically synthesize the callback/result threading and optional failure behavior. Added row/store declarations, shared hooks and final typed-field adaptation are authored and retained in `implementation-patch.json` and the final source commit. The recipes record intermediate migrations, not an executable whole-change equivalence proof. Empty guards/parse-clean simulation do not establish behavioral equivalence; the executed boundary tests are the behavior evidence.

Scan command:

```
PYTHONDONTWRITEBYTECODE=1 XDG_CACHE_HOME=$PWD/.artifacts/cache timeout 165 \
/home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor \
src/agent_comms/relationships.py src/agent_comms/passive_channel_awareness.py \
src/agent_comms/locked_store.py --context-root src/agent_comms --json --json-payload loop \
--parse-workers 1 --analysis-workers 1 --no-cache
```

`process-evidence.json` records actual resumed session/process state. `cleanup.json` records removal of only this worker's disposable artifacts after test/audit processes exited. Original worktrees, commits and durable recipes/evidence are preserved.

Evidence text logs have trailing whitespace normalized; JSON patch retains exact diff bytes.
