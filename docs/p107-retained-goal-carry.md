# P107 restoration and retained Goal carry

Owner: core-goal-retry. Codex alone executes publication, including private
pointer-switch acceptance. Draft #1145 restores the normal P107 revert and
extends the existing stopped-owner installation; #1143 is superseded.

## Current checkpoint

- Source feature remains the accepted +520 src version. Carrier changes are
  tools only; no alternate production reader or default-author fallback.
- Installed ff5bcd2aa authenticates original Goal JSON. Installed c8c4eb53f
  decodes candidates. All 37 current Goals, 529 observed history rows (earlier
  inventory: 528), and 163 original release-receipt slots survived the copied
  conversion. See `.artifacts/p107-format-journey.log`.
- History conversion changes only before/after Goal cells. Sequence, kind,
  owner, timestamps and pending/committed/aborted/uncertain states stay exact.
- Registry, history and `OwnerReleaseReceipt.thread` share one conversion.
  Source-less Goals remain source-less; no mention binding/author proof is
  invented. Old progress strings remain strings; existing authored reports stay
  exact. Existing Todo stores refuse rather than invent acceptance/assignment
  facts. The inspected source root has no Todo store.
- Existing `GoalNativeStore` now owns ledger declaration/synchronization; this
  no longer imports deleted compaction models to inspect an unrelated ledger.
- `CarryGoalFormatsRuntimeInstallation` stops through the original decoder.
  The existing `OwnerRestartHandoff` and inherited wire/route descriptors cross
  the private stdin pipe to the target decoder. Credentials are never saved.
- `ReviewedInstalledRuntime` extends the ACTUAL `RuntimePublication` pointer
  owner, not the obsolete SDK-0.12.1 activation format. The existing publisher
  switches declaration-derived launchers once, after conversion and CLI readback.
- Registry publication uses `RegistryStore._write_unlocked` (private guard).
  All acquired preimages, including the guard, are retained before any write.
  Installed Goal semantics are compared with those actual backups.
- Target launch derives from the reviewed runtime, not a still-old global
  pointer. No second fence/stop/launch algorithm, native input or queue replay.

## Verification so far

Before AST: 1068 modules, no omissions. After: 1072 modules, no omissions,
including actual paired installed Toad. Syntax cannot resolve arbitrary dynamic
MRO/callbacks. Both installed codecs round-trip the new review packet and validate
the private source route/launchers. Focused restored Core checks: 21 passed.
The new stopped conversion/target-launch path is NOT YET accepted. Exact
`6b829762f` was separately attempted by Codex and refused before stopping, first
on the rootless public bridge and then on the existing Toad launcher; operation2
and operation3 candidates/records were not created. All saved files and both
pointers remained unchanged in each attempt. No input was replayed.

The existing preflight now reports its COMPLETE observed refusal list in one
error, including unreadable processes instead of treating them as absent. It
retains exact original process-group membership and the existing client guard;
no argv/root guessing or blanket exemption was added. Codex must retire clients
through their actual owners, preserve their state, run one reviewed private
attempt, then restore them (Toad via `/home/ts/bin/toad-comms`, DISPLAY=:0).
Other agents' verification clients require their owners' disposition, not a
blind process kill. Latest observed list: `.artifacts/p107-complete-client-list.log`.

The first
operator attempt blocked BEFORE stopping: the publisher requested an exclusive
route lock while the running worker's observation child held its lifetime shared
lease. Codex interrupted only the waiting publisher and verified all four saved
files and both pointers unchanged; no candidate or operation record was created.
Evidence: `stopped-proof-1/operator-result.json` and `operator-blocker.json`.
The repair keeps a shared route lease during stopping and upgrades through the
existing publisher only AFTER all-stopped ownership. Both normal and cross-codec
publication paths use that owner. Original route/pointer facts are rechecked
under exclusive access. A new attempt requires Codex's explicit review/execution.

Fresh private root and real idle SDK-fork worker are prepared under
`/home/ts/.cache/agent-scratch/gr107/stopped-proof-1`; source worker receipt is
`prepared.json`. All retained Goal data/history/receipts come from frozen real
store copies, not invented messages. Imported participants have no processes or
active turns in the NEW root; that deliberate cold-import change is recorded.
Only `p107-copy-worker` is running. No provider input was submitted. The original
sessions, public data and shared pointer remain untouched.

## Exact private acceptance command (Codex only; one attempt)

```bash
env -u PYTHONPATH -u AGENT_COMMS_ROOT -u AGENT_COMMS_THREAD \
  -u AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID -u AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE \
  HOME=/home/ts/.cache/agent-scratch/gr107/stopped-proof-1/home \
  /home/ts/.local/share/agent-comms/runtimes/toad-58d65de58-core-c8c4eb53f-native-70f0512abbcfa4e0-20261017T061350Z/bin/python -I -c \
  'import sys; sys.path.insert(0,"/home/ts/wt/core-goal-retry/tools/cutover"); from publish_runtime import main; main()' \
  --carry-goals \
  --candidate /home/ts/.cache/agent-scratch/gr107/stopped-proof-1/candidate4 \
  --receipt /home/ts/.cache/agent-scratch/gr107/stopped-proof-1/operation4.json
```

Do not repeat a failed attempt. Inspect its original receipt, preimages, process
and current pointer before disposition. Any changed/uncertain result stays
stopped for review, never implicit rollback or replay. After success compare the
37 Goals and all history/release rows to backups, verify guard, source exit,
exact target worker prefix and unchanged saved-fork session/configuration. Then
exercise the real target native session load. Public execution still requires
Codex review and a fresh paired build containing the final source.
