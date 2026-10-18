# Retained upgrade ownership

Integration owner: codex-agent-comms. User explicitly requested current mains and proceeding with the retained-bus migration. Live Core stays editable from `/home/ts/code/projects/agent-comms`; no development checkout is the live runtime.

## Actual blockers repaired

- `native_package._resource_root` mistook a checked-in `_native/core_stamp.py` build artifact for packaged resources. That artifact names a different checkout and has no consumer (`git grep '_native.core_stamp'` returns none). Source resources are owned by stack; installed resources by the packaged directory. Source selection now precedes packaged-artifact detection; the unused stamp copy is deleted. `package_stamp.STAMP` remains the source/wheel identity owner used by `owner_protocol` and the build hook.
- All listed native-file hashes still match. Preparing the complete artifact produced a different full dependency-tree commitment; only that final manifest commitment was refreshed after verification. The stock Pi package is unchanged.
- The existing one-shot carry CLIs acquired their own wire lock inside the migration algorithm, which deadlocks a retained stopped-owner cutover already holding its original OFD. The algorithms are now `carry_locked`; each CLI still acquires wire at ingress. The existing `StoppedOwnerBatch` retains wire for the target's three calls. No second lock store, codec, fallback or runtime schema reader is added.

## Source closure evidence

NRA `ModuleSyntaxIndex` indexed every original declaration/import/write/decision/call in the three carry modules before editing; after evidence lists their declarations and lock calls. Zero parse omissions. Files: carry-ast-before.json and carry-ast-after.json. Dynamic operation imports use exactly these three filenames and were read semantically; this is not a claim to resolve arbitrary dynamic imports.

Before: carry_storage_roots.carry and carry_goal_publications.carry each acquired wire internally; goal waits had its algorithm inside the wire-owning CLI. Their only executable callers were those CLIs. After: each has one `carry_locked` algorithm; the original CLI and target.py consume it. Wire ownership is either the existing CLI context or the existing retained `StoppedOwnerBatch`, never both. Search: `git grep -n 'carry_locked\|_store_lock' -- tools/cutover upgrade-evidence/operation/target.py`.

The two Core identity declarations remain distinct source/wheel cases. Deleting `_native/core_stamp.py` removes an unused absolute-path copy, not a required identity. Search: `git grep -n 'package_stamp\|_native.core_stamp' -- '*.py'`.

## Verified before live cutover

- 28 native-package tests passed, including source build-artifact interference and installed missing-resource failure.
- Candidate native package prepared and whole-tree verified. Candidate ACP/Toad imports work.
- On a disposable copy of the retained root, all 13 storage records carried, publication/wait carry completed, and the new RegistryDocument decoded. A second rehearsal included an original-format goal: both copies of its goal record carried, and new Core read the migrated goal and retained input dispositions. Original root untouched by rehearsal.

## One-shot operation

original.py executes with the authentic original source under `/home/ts/wt/comms-update-original` and original installed interpreter. It acquires the existing Draining barrier, uses registry filesystem notifications to wait for active turns to end, then invokes the existing maintained stopped-owner cutover. It does not poll for replies or interrupt an active turn.

Only the all-stopped callback closes the old Toad frontend normally, takes the original route lock, preserves a quiet full root preimage, integrates reviewed main, syncs packages, installs the verified native artifact into main, and calls target.py with inherited wire/route descriptors. The handoff remains in RAM/stdin; credentials are not written to evidence.

target.py uses the existing carries, validates the retired owner witnesses, atomically updates the existing route (same root ID), clears the exact maintenance hold, and restores only the previously running owners. Original sessions, stopped agents and uncertain input dispositions are not replayed or discarded. verify.py checks JSONL retained prefixes, runtime entry points and official ACP attachment without sending a prompt. The standard persistent-goal owner continues this author's actual physical Toad acceptance after restart; no new queue or reminder store is created.

A failure records an exact receipt. The existing cutover recovery owner may restore the authentic original runtime only when main source, its native manifest and the original registry schema are proved unchanged; otherwise stopped custody is relinquished with maintenance held. No uncertain publication, launch or input is retried. Original preimages, operation source and receipts remain until acceptance. Do not delete them while this operation or resumed author needs them.
