# Canonical owner instructions — PR531

## What changed

The three existing instruction documents were taken from the already authored
PR432 branch at `ff1a8f75caeda9b6fe106bce386cf13375d16efd`: `.pi/APPEND_SYSTEM.md`,
`AGENTS.md`, and retained task memory `00-RULES.md`. No other PR432 changes were
imported. They put source reasoning, existing behavior owners, whole consumer
migration and deletion before batched checks and the installed application.

The existing native resource owner now supplies `OWNER_INSTRUCTIONS`.
`NativePiRpcLaunch.bootstrap` passes that canonical file through Pi's existing
`--append-system-prompt` capability for ordinary, managed and tracked launches.
The source stack uses a symlink to the authored file; the normal wheel includes
that same file as a resource. There is no per-agent guidance copy or loader.
Missing resources refuse before launch because Pi otherwise accepts a missing
file argument as literal prompt text. The ordinary console caller also now
passes its existing `RestartEnvironment` to the shared bootstrap owner.

The original SDK loader uses an explicitly supplied append source instead of
discovering a cwd append file. Project context files keep their existing loading
contract. No discovery registry, content deduplication or path hash was added.
The relevant loader, argument, service and main modules are byte-identical in
5184 and the qualified ad533 package actually used for inspection.

## Source closure

The before/after AST maps and search output are under
`evidence/canonical-owner-instructions-20261002/`. The existing NRA mapper parsed
918 files before and 919 after normal main integration; its one unsupported
Python 3.14 file, `tracked_turn.py`, was parsed with the existing candidate's
Python 3.14 standard AST. There are no remaining parser omissions. The final
map records 18 related declarations and 256 candidate consumer calls.

Those references are mapping evidence, not proof of dynamic resolution. Existing
launch families, their callers and the SDK loader were read semantically.
JavaScript SDK contracts are outside the Python AST scope. Source search finds
one instruction resource declaration and one append decision in bootstrap.
This change introduces no class or competing configuration owner. The seven
source/guidance files remove ten lines, including the old tests-first ordering
and the incomplete ordinary bootstrap call.

## Authorized placement

All three placed paths resolve to bytes with SHA256
`d8313217c468a2b317802ebc5a87d4cec606c689e6d1cfd26bbdd2bb052a2cc6`:

- `/home/ts/.agent-comms/.pi/APPEND_SYSTEM.md` — canonical live file.
- `/home/ts/.codex/AGENTS.md` — symlink to that file.
- `/home/ts/.pi/agent/APPEND_SYSTEM.md` — symlink to that file.

Original live/global files and their hashes were retained before placement in
`/home/ts/.cache/agent-scratch/dedicated-worktree-cleanup-20261001/canonical-instructions531-20261002/`.
Placement waited until Arendt's configured qualification stopped borrowing the
files. Other projects' AGENTS files were not changed. Existing agents received
the owner's ordering directly; no provider process was restarted for placement.

## Installed inspection

Source `c19d02cd02c1d21465b6e298d3502b06656996a7` normally integrates main
`d6e196a8`. Its normal wheel and sdist retain the exact authored bytes. Only the
small Core wheel was installed into an owned target using existing dependencies;
there was no new full environment or native build.

After public324 owners were running, the existing `RetainedOwnerLaunch.capture`
read the actual fenced owner processes. Two private no-input children retained
their original cwd, model, thinking selection and configuration discovery, with
owned writable session resources. Only `get_state` and
`agent_comms_inspect_context` were sent:

| Captured owner | cwd append exists | Canonical file/content count | Discovered cwd append count | Elapsed |
| --- | --- | --- | --- | --- |
| nra-architecture | yes | 1 / 1 | 0 | 1.325 s |
| openhcs-helper2 | no | 1 / 1 | 0 | 1.325 s |

Both inspections passed and both private children closed with empty stderr.
Receipts contain file provenance and hashes, not prompt bodies or credentials.
There were zero prompt commands, provider calls and public mutations.

Two earlier pre-input refusals remain recorded: the first encountered the
parent's temporarily stopped owner; the second was a driver argument error
before process creation. The driver now uses the existing
`ThinkingLevel.optional_name` projection, as the ordinary turn runner does.
Neither refusal sent input or launched a child.

The public default remains the parent's Core529/Toad322/nativead533 release.
This inspection qualifies packaged PR531 loading; it does not claim that
already running public owners have adopted Core531. No public prefix, saved
session, uncertain input or parent UI client was changed.
