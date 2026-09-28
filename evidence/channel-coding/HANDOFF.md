# Normal channel coding tools — parent implementation

Branch: feat/channel-coding-tools-20260927. Default FULL selects normal Pi
read/bash/edit/write. TRIAGE remains no-tools. The explicit selected one-write
proof path and operator-owned file plans retain their separate contracts.

Pi owns tool execution and full argument schemas. CodingTool declarations own
only cooperative resource policy; the old SelectedToolSocket transport is now
an OwnerToolSocket ABC shared by both policies. Native input/context proof and
the exact live selected owner are required before tool admission. Edit and
write acquire/reuse the existing bus claim projection, including typed missing
file paths for creation. Completion releases exact observed generations under
one wire/bus/registry boundary. Bash remains cooperative, not an OS sandbox.
No duplicate backend, file executor, claim store or scheduler was introduced.

The normal copied native build packages and admits the coding extension.
stack/bin/prepare-pi-native reproduced the pinned tree successfully.

## Actual acceptance

`live_coding_acceptance.py live-coding-native-schema` ran the configured
openai-codex/gpt-6-sol model through unmentioned channel TRIAGE then FULL in a
separate private test bus, with work in this persistent ~/wt worktree.
Actual native transcript calls were read, edit, write, bash. The existing file
changed BEFORE to AFTER, nested/result.txt was created with matching content,
Python assertions passed, both resource claims were released, and the channel
reply was CODING_TOOLS_OK. Elapsed 31.748 seconds. See the JSON receipt.

Earlier attempts are retained, never replayed:
- Preflight rejected the build path before any native input reservation.
- Actual provider IDs contain '|'; coding ledger keys now derive from full
  input/call identity while preserving the original ID for RPC correlation.
- The initial completion helper nested a non-reentrant wire lock; the bounded
  acceptance process timed out. Release now shares the existing canonical
  selected-claim boundary without nested flock acquisition.
- Pi edit now accepts edits[]; the Python policy no longer mirrors the native
  argument schema. It checks only the mutation path and preserves exact args.

A real continued-context check found older TRIAGE proof verification selected
the latest context generation rather than its independently recorded generation.
Readers now select the recorded generation while validating the whole journal.
Actual TRIAGE and FULL evidence both corroborate after four coding tools, with
no SQL input changes/replay. 89 context/binding/continued tests passed,7 skips.

NRA current scan analyzed all79 detectors, omitted0, exact_compact_global,
complete, zero findings on the requested policy/claim paths. This preceded the
last release-boundary/context-reader fix; no final whole-package clean claim.
The first scan collided with edits and failed; no result was claimed for it.

## Remaining before live activation

Merge current main (S2 PR143 and history PR144), resolve native imports while
preserving S2's common RPC reader, then run the focused merged selections.
Install the paired core/native package; retain current history Toad and Textual.
Restart idle owners using the normal guarded API and test fresh installed
channel coding in an owned worktree. Production still uses runtime-history
with the prior native package; none of this coding change is live yet.
PR95 repeated real compaction and queued input acceptance remains separate.

## Integration complete

Merged main ee774f1 (S2 PR143 plus history PR144). The single native event-loop
conflict now dispatches S2 nominal ToolExecutionStart/End/AgentSettled values
into the shared tool policy. 144 focused merged tests passed,6 optional skips.
Core wheel installed in runtime-coding-20260927 with current history Toad79.
Parent next activates the paired native package and performs fresh installed
owner delivery/tool checks. No CI gate.
