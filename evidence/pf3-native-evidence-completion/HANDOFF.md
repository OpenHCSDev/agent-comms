# PF3 complete native evidence ownership

## State and source

Complete PF3 source in the owned persistent tree
`~/wt/comms-pf3-native-evidence-completion-20260928`, branch
`refactor/pf3-native-evidence-completion-20260928`, based on the parent's committed
`af2e04c`. The full branch includes that implementation and its parent notification
work; those inherited commits have not been reconstructed or edited here.
Parent retains serial integration, packaging, live UX and deployment.

No remaining PF3 implementation blocker. No live modifications, replay, provider
requests, package rebuild/mutation, additional agents or model changes.

## Complete owner/caller/deletion map

| Owner | Complete replacement and current consumers |
| --- | --- |
| `NativeEntry` / `SessionEntry` / `MessageEntry` (`native_entries.py`) | Decode private session entries behind the existing private-file boundary; validate header, unique tracked input IDs, user role, input digest and failed-terminal parent/role/content. Used by digest lookup, context proof reading, continued history and dead-native failure recovery. Presentation's tolerant decode is not substituted for these proof checks. |
| `NativeContextProof` (`native_pi.py`) | Own strict journal shape derived from its fields, input/generation/digest checks, saved session/entry joins and ordering. Both live-event verification and historical corroboration consume it. Removed duplicated raw header/user scans and chosen-row mirrors. |
| `FreshPrivateSession` (`fresh_private_session.py`) | Own launch header denial check and existing process/inode/bootstrap identity checks. `prepare_native_pi_rpc_launch` now calls this owner instead of interpreting the raw header and fresh marker. Construction remains private, process-local, and impossible to reconstruct from saved bytes. |
| `SelectedFreshMarker` (`native_entries.py`) | Declares the existing schema/thinking-level saved shape with FieldCodec validation, shared by header decoding and the existing fresh producer. Rejects unknown fields and bool-as-version; no extra registry or store. The marker remains denial data, never enrollment authority. |
| `PiContent` / `UnknownContent` (`pi_payloads.py`) | Preserve every user-content field at the strict evidence boundary. An extended or unrepresented part stays opaque and cannot compare equal to `TextContent`; actual unknown content serializes through its existing payload owner. |
| `continued_private_session.py` | Uses typed header/tracked users and exact typed text/digest match against independently recorded STARTED data, or exact independently recorded native context proof. Keeps all UNKNOWN rows unchanged. |
| `coordination_store.py` | Recovery uses typed entries and the failed-terminal owner; requires the original prompt binding, exact parent, one terminal, released owner and exited native process. It still cannot reconstruct acceptance or resolve publication uncertainty. |
| Existing `native_prompt_binding.py` / `historical_native_inputs.py` callers | Continue to use the migrated digest/context reader owners. No second parser, entry family or history store added. |

Deleted `load_native_context_proof` and its obsolete interface-only test; actual
negative recovery-authority tests remain. Native saved spellings/formats remain
unchanged; there is no saved-data migration or compatibility adapter.

## Strict equality audit and substantive correction

The inherited `NativeEntry.from_evidence` encoded user parts and rejected all
unrepresented fields. That prevented lossy STARTED text equality, but also
rejected extended user content which the previous journal-corrobation path could
read. The completed boundary preserves such parts using the existing
`UnknownContent` owner instead. Thus extra fields, explicit null signatures,
signed text, unknown parts and string-vs-array differences cannot falsely match
the original exact single-text STARTED contract. Independently live-recorded
context proofs can still corroborate extended content without granting execution.

New negative/positive tests exercise both actual continued-session branches:
invalid text projections do not reserve summaries; corroborated opaque content
does reserve while the raw UNKNOWN marker remains UNKNOWN. Non-user tracked IDs
cannot disappear into display's unknown-role fallback. Duplicate tracked inputs
remain rejected. Malformed/mismatched fresh markers fail before private launch
settings are published. The selected first-source native guard is unchanged.

## Local evidence (overlapping batches, do not add counts)

Parent receipts copied unchanged under `parent/` from the assigned source tree:

- `fourth.log`: **110 passed, 6 skipped**, includes strict proof, continued
  history, store recovery and transcript cases.
- `native-and-recovery.log`: **107 passed, 2 skipped**, actual reviewed copied
  native CLI with loopback provider, recovery/prompt binding and payload cases.
- `consumers.log`: **26 passed, 23 skipped**, including source cursor **5 passed**
  and actual selected native four-tool publication/release. All 21 selected-owner
  integration cases were initially skipped because their bundle opt-in was unset.
- Earlier parent first/second/third failure logs and lint receipt are retained;
  they are not claimed as successful acceptance.

Completion receipts:

- `boundary-first.log`: **129 passed, 8 skipped**, new strict-boundary tests plus
  continued/fresh session, native proof, transcript and payload coverage.
- `continued-final.log`: **40 passed**, including final independently recorded
  extended/null/opaque-context cases and all new admission/equality negatives.
- `fresh-native.log`: **2 passed**, enabled actual reviewed copied SessionManager
  selected bootstrap read/append and ordinary fresh-inode preservation.
- `selected-owner-current.log`: **17 passed, 4 skipped**, enabled actual native
  RPC/SDK selected-summary commit, correction/decline, one original admission,
  effective settings and no-goal owner cases. Remaining four combinations are
  intentionally impossible fixture variants: private coverage with the synthetic
  non-SDK host. Real SDK private variants passed. Outbound fetch is forbidden by
  both existing native fixture scripts; no paid/configured-provider calls.

The first enabled compaction attempt (`selected-owner-enabled.log`) failed 17
cases at the real launcher/Python manifest fence, using the obsolete prepared
stack. The fixture now accepts the existing `AC_NATIVE_STACK_BIN` setting and was
run with this tree's current stack launcher plus the current reviewed package.
No production fence was relaxed and no obsolete API was restored.

Exact package for successful native cases:
`/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent`.
The owned stack's `.pi-native-50e477b6db64167e` ancestor temporarily pointed to that
existing package root for read-only reuse. Manifest and complete-tree validation
ran normally. The alias is removed after testing; the shared package is untouched.
To reproduce: recreate that owned ancestor link, set `PI_COMPACTION_TEST_PACKAGE`
to the package above and `AC_NATIVE_STACK_BIN` to this tree's `stack/bin/pi-native`,
then run the recorded command. Fresh tests use `AGENT_COMMS_TEST_COPIED_PIN`.

## Actual saved-source audit (read-only)

`inspect_saved_entries.py` reads original saved sources privately and writes only
aggregate counts and hashed source references. `saved-entries.json` records:

- **77 session files, 55,250 entries, 7,525 user rows and 243,919 proof journal rows**.
- Zero typed decoding failures or user-content/input-ID/digest/proof-field
  mismatches. Includes both original owner sessions and two private-root histories.
- All observed session inode/size/mtime revisions remained unchanged during read.
- No source content copied into public evidence; zero live writes/provider calls.

This is a shape/content preservation audit, not acceptance, fsync evidence,
execution permission or a bypass of the existing bounded strict reader. Issue107
journal growth/bounds remains explicitly outside PF3; no limit was raised and no
retention protocol was invented. Live event/proof matching and caller revision
fences remain mandatory. UNKNOWN is never promoted or replayed by parsing.

## NRA and limits

`nra-final.json`: explicit whole-package context, exact compact global analysis,
all **79 detectors**, zero omissions, complete, no reported findings for selected
PF3 files. The initial verbose scan timed out at 165 seconds; its receipt remains.
Changes are authored owner/caller migrations, not a claimed codemod equivalence
certificate. Behavioral and installed readiness are distinct from scan results.

Focused lint and whitespace checks passed. Local acceptance is complete; parent
owns the installed/live boundary and urgent DM/activity work. CI remains deferred.
Owned caches and package alias are cleaned; branch, scripts and receipts remain.
