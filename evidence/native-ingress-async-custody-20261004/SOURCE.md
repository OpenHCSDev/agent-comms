# One native ingress owner

Original #633 run03: same PID155773 held exclusive wire inode3841569 while its
main loop waited for shared custody. InputDrain.queue_followup retains exclusive
custody across joined reservation/rollback work. A concurrent synchronous native
send blocked the loop needed to receive that result and release the exclusive
resource. Original kernel/identity/exit evidence remains on #633 d4e0ad4f.

MaintenanceBarrier.admit_ingress_async now acquires the original shared resource
once for backend initial prompt, InputForwarding.send_prompt and interrupt.
Delegates retain exactly their original registry/goal/input/journal/source checks,
durable binding and synchronous stdin.write inside that resource; drain/ACK and
provider work happen after release. Cancellation during acquisition never enters
the delegate or writes bytes. There is no reentrant registry/cache, timeout or
retry. Original selected PrivateSendAdmission/TrackedTurnSession raw-worker
custody remains unchanged.

OwnedSendAdmission now consumes that acquired ingress instead of independently
acquiring it. The _maintenance_wire_locked marker and getattr dispatch are gone.
NativeStartupAdmission retains its original root; its directory is derived from
that root. All three sends use that same startup root, deleting the backend/input
copies of environment/fallback selection. Thread.native_environment originally
supplies the Comms root for owned turns; explicit selected startup retains its
original root. No root identity is inferred from directory positions.

Original Package.load parses all production and test roots with zero omissions.
Before/after declarations/imports/calls are retained. Custom synchronous delegate
callbacks are a dynamic boundary, not proved by AST. All production callers and
two direct maintenance controls are migrated. The added physical-lock control
checks that an exclusive async owner can resume and release while shared send
waits; the existing maintenance control still prevents bytes through closed
admission and waits through the final synchronous write.

Final affected installed qualification is committed in #633 at 1a05c7e9,
evidence/native-source-turn-publication-20261004/. Production here is byte-equal
to that normally joined #638+#640+merged #637 source; this does not claim an
old standalone package qualification. The source owner batch deletes 85 lines
and adds 71 across four production files.

Original physical shared/exclusive ingress, exclusive final-write, interrupt,
cancellation isolation and hard-exit UNKNOWN controls passed in the preserved
#638 original 11-pass/16-fail result. Those accepted controls were not repeated.
Changed source640 installed01 passed native queued ACK/start and four retry
paths/exact source/replacement fences (16 pass/4 fixture fail). Original02
remains 2 pass/2 fixture fail; final03 only two corrected cases passed, exit0,
16.623298s. Original FULL wire publication, native context receipt, distinct
retry lease/goal pause/unchanged UNKNOWN and all waiter/inbox/child cleanup pass.
The previous mixed receipts remain mixed; no failure is relabelled or replayed.

Normal wheel58a11a61 has 347 source/wheel/installed members exact, 84 unchanged
environment/dependency keeper files. Actual localhost HTTP/native/ACP scope,
nonzero native inputs, external/paid/public/replay zero. Exact recorded child
and controller identities/groups absent at joined terminal; Bohr independently
closed whole540 purpose with 253 all-UID processes/zero borrowers/zero gaps.
Immutable086 read/execution returned. No timing or public-channel claim.
Normal merge #638 then #640 then fixture #633; parent owns publication.

## Correlated native preflight, 2026-10-07

Original live failure diagnostic:
/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/cc5fd116d4b74b50811b4fa8d0533d5c.json.
It attests NotSent during native preparation, records the original get_state
command and empty stderr, but does not retain the unexpected event. Its exact
type cannot be reconstructed. Neither that input nor the manual resend is run
again by this change.

The concrete source defect is requiring the next channel event to be the state
reply. Selected immutable Pi rpc-mode.js produces unsolicited extension UI
notifications/status/error events and binds extensions before serving commands.
NativeQuery.exchange already correlates multiplexed replies via PendingRequests.
Both streaming and tracked preflight instead assumed the next event matched.

PiEvent/Response now own the nominal command/id response relation, also consumed
by require_request. PendingAttestation changes state only for its own matching
successful response, with the unchanged capability/payload/expected-session
checks. Its generic observe no longer independently grants attestation from an
unrelated GetState reply. Streaming and tracked consumers continue ordinary
event processing until this response. AgentSettled owns the shared distinction
that prior completion while attestation is pending cannot settle this new turn;
both event consumers use it. The watchdog keeps the original absolute startup
deadline ahead of optional stats waiting. No timeout is raised or renewed.
The current installed initialization pilot uses existing NativeQuery.exchange
instead of assuming its next receive is the reply. Frozen evidence is unchanged.
Native custody compaction reattestation/catalog discovery already use exchange
and need no alternate receiver. No message_bus.py/turn_context.py edit.

Complete source traversal parsed/compiled 759 src/tests/tools modules without
imports or omissions; dynamic external consumers remain unresolved. Nine
focused checks passed: pending promotion, unsolicited status/old settlement and
foreign response followed by exact reply, plus original wrong-ID/EOF/invalid
data, missing capability and no matching preflight-before-user-start refusals.
These are existing authored subprocess protocol controls, not native SDK/live
qualification. The positive fixture initially lacked the required assistant
content array, reached that later strict decoder and failed; only its fixture
was corrected and that check rerun. Eight other passed checks were not repeated.
No public start/stop/restart, SDK/provider operation or failed-input replay.
Parent must integrate/build/deliver before claiming the live failure is repaired.

## Native publication after a declared project change — 2026-10-07

The original agent-comms-ux SDK journal records comms_set_project success at
2026-10-07T16:03:50.221Z, from /home/ts/.agent-comms to
/home/ts/wt/comms-post-feature-debt-audit-20260928. The diagnostic
/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/990b6fdf18304b78b18bb54156bc18a6.json
then refused native source publication on registry_worktree. This is a proved
declared project transition, not the earlier unknown SQLite blocking writer.
The uncertain original input remains untouched and must not be replayed.

ThreadManagement.set_project preserves process, conversation and active turn,
records the former directory and directs end-turn continuation into the new
project. OwnedTurn.close_changed_project/continue_changed_project require that
old/new distinction. RegistryAdmissionCheck previously applied the send's exact
directory fence to source publication, and Registration returned the complete
current directory, which would erase that distinction if the check alone were
relaxed. LiveResponseOwner also used that same send check at reply publication.

RegistryPublicationCheck now shares all original incarnation/process/admission/
role/model/thinking/session/exact-lease checks through the declaration family.
Its directory predicate requires the captured project to remain among the same
conversation's canonical directories. Strict input admission retains exact
worktree equality. Thread.contains_worktree owns the existing saved-session
directory relation; SessionLifecycle.validated_thread uses it rather than
rebuilding that answer. Source publication and response publication select the
publication check. Registration publishes against the current document while
returning the admitted operation's original executing directory, preserving
current source/phase and end-turn old-to-new continuation. No durable field,
codec, alias, cache or source-format change.

Two actual private-registry checks passed in 0.24s: declared project change plus
repeated source publication preserves the captured directory/lease and current
next-project directory; another input still refuses; original reply validation
accepts. An unrecorded directory replacement still refuses source publication
without writing the registry. The first check location imported the unavailable
ACP development dependency and stopped before collection; these original
registration operations were then checked in their dependency-independent
registration test family. No environment or installed package was changed.
Raw logs and source sites:
/home/ts/.cache/agent-scratch/mendel-project-source-publication-20261007.
All 759 src/tests/tools modules parse/compile; 510 related declarations/calls/
worktree writes enumerated, no parse omissions. Dynamic external writers remain
unresolved; registry/process/source fences remain authoritative. No SDK, live
attach, restart, provider input, build, publication or failed-input replay.
Parent carries coherent installed delivery and affected live verification.

### Actual private native/ACP project transition

Migrated the existing test_native_project_observation control from /bin/echo to
NativeBackendFixture, its original validated Pi launcher, private SDK journal,
RuntimeServer and LoopbackProvider. The real provider request invokes the
original set_project operation while the first lease is executing. The original
wake owner consumes the successful turn's queued continuation under a distinct
lease in the new project. No fabricated native events or terminal results.
The committed 4b fetch-origin policy is bound to the fixture's actual localhost
origin; proxy variables are removed. No guard qualification was repeated.

The first attempt opened its native child but exposed a second stale test API:
TurnRunner.native_environment no longer exists. Thread already owns that method;
the sole stale test caller now uses it. The attempt made zero provider calls and
retired its child; all raw data remains in /home/ts/.cache/agent-scratch/mpj01.

The changed helper ran once in /home/ts/.cache/agent-scratch/mpj02. Both real
localhost requests and committed assistant replies completed. Assertions reached
and passed: first ACP prompt ended normally, original scheduled continuation
joined, project mutation occurred once under the first executing lease, second
lease used the new directory, two USER entries used the same SDK journal,
continuation text was original, session identity stayed unchanged, final project
was current, no active lease remained, and only one thread existed. The helper
then incorrectly asserted two context rows. NativeContextRecord's declared key
is (input_id, request_generation), so these two inputs correctly produced three
context observations. The assertion now checks two distinct input IDs. The
original retained journal/proof was checked through the existing evidence reader:
two completed assistant replies, two distinct input IDs and the original project
continuation. See mpj02/retained-result.json and check.log. Native inputs were not
repeated just to make the helper green.

The pytest terminal remains FAIL at that helper assertion, not a native failure.
Later socket stale-generation/process/foreign-name assertions were not reached;
the native transition result does not claim them. Recorded child retired and is
absent; canonical owner shutdown joined and released its backends. Previous raw
failures/UNKNOWN input are untouched. All 759 modules parse/compile, no omissions.

Execution used system pytest with source src/tests ahead of read-only retained
runtime dependency paths; this qualifies source with real native/ACP behavior,
not an installed successor wheel. Parent still owns build/publication. No installed
package mutation, backend restart, public participant/input, paid request or new
native assembly. The two authored completed native inputs and raw keepers remain
held under the persistent mpj02 root.

### Installed backend and default delivery

The built integrated2072058 backend was checked through the corrected original native/ACP control using installed modules, fresh private SDK journal and two localhost requests. All assertions passed, including original project continuation and stale generation/process/foreign-name refusal; native child retired. Raw: /home/ts/.cache/agent-scratch/installed-project-turn-20261007/check-prepared-root.{stdout,stderr}.log, private journal retained under /home/ts/.cache/agent-scratch/mpj03. Earlier pytest-plugin refusal and overlong socket-root refusal occurred before input and remain held. The short root first needed its parent prepared before fixture creation; no native operation occurred in that setup refusal.

All954 assets/full69 distribution versions and2857 RECORD rows matched. The original ReviewedCommsBackendCohort publisher changed future backend defaults to .artifacts/project-turn-delivery-20261007/runtime; the current frontend/native route and existing running owners were preserved. This is installed/default-delivered, not proof that existing agents loaded the successor. Original user UNKNOWN input and blocked goal were not replayed or resumed.

## Installed idle-worker restart — 2026-10-07

The actual old menu-send interpreter launched an authored private idle worker.
The installed project-turn successor used existing OwnerRestartRequest and
PreserveOwnerRuntime: capture authentic original launch, fence idle admission,
stop and join the old process, then launch through StoppedOwnerBatch with the
successor interpreter. No production owner, format, runtime wheel or native
package changed for this control. Original restart/lifecycle/launch/input/goal
owner sources and Thread durable declarations match across these two runtimes.

The original NativeBackendFixture supplied a fresh SDK journal and private
settings/worktree. The goal was authored and blocked through original goal
actions. A genuine retained NotSent observation from closed m687n02 was copied
through InputDispositions into this new private root. Its original file/root and
uncertain input were untouched; the observation was never submitted as input.
This proves retention/non-replay in the new private restart, not recovery or
redispatch of that historical input.

One installed check passed in 15.87 seconds. Old worker 3819493 became successor
3819865 through the existing restart owner. Actual attachments loaded both
workers' original sessions. Journal/settings/auth/models/input-document bytes,
worktree/session/incarnation, blocked goal, native arguments and config directory
were unchanged. There were zero localhost POSTs and zero native inputs. Cleanup
stopped/joined the replacement through the original owner; final direct read
found both processes absent, owner retired and no runtime sockets.

Execution used successor installed Python/Core and dependencies, read-only system
pytest and existing source test helpers. The old setup cleared PYTHONPATH and
used the actual old installed interpreter; replacement launch likewise carried
no source overlay. No install/build/public restart/provider input occurred.
Raw output and final owner release are retained at
`/home/ts/.cache/agent-scratch/mir01/check.log` and `result.json`.
Parent owns live reload; this private result does not update existing public
workers or authorize replay of their uncertain inputs.

## Restart launch refusal before retirement — 2026-10-07

Parent's live reload exposed deterministic missing private launch authority only
after the original process had stopped. PreserveOwnerRuntime installs nothing;
its target authority must already exist. Installation cutovers have a different
contract: bind_target_launch supplies their target after conversion/installation.

OwnerLifecycle.require_launch_authority now owns the original private-marker/pin
refusal and PrivateNkLaunch validation, extracted from _launch_owner_unlocked.
Actual launch still calls that same check before process reservation. OwnerCutover
declares pre-retirement launch admission; preserving members require the existing
authority and installation members retain their existing later target binding.
AdmittedOwnerBatch asks its operation before registry fencing or any signal.
An empty selection remains a no-op. No duplicate pin state, format, environment
decoder, recovery algorithm or concrete-type dispatch was added.

The original real private owner control passed once in 6.87 seconds. A distinct
unpinned Comms instance refused preserving restart while the original process
remained alive, the entire registry snapshot stayed equal and no release receipt
appeared. The original pinned restart then passed. The same control exercised an
unpinned StoppedOwnerInstallation whose genuine post-stop bind_target_launch pins
the target; it launched normally, and final original stop retired the replacement.
No provider/native input/public operation/build/install occurred. This is source
with real original worker processes, not installed delivery of the correction.

Existing refactor-audit Package parsed all 759 src/tests/tools modules with zero
omissions; 313 before/after related declaration/attribute/call sites were retained.
Complete production installation consumers include retained-index, retained-task
carry, extension activation, recovery publication, retained-summary publication
and cross-runtime thread retirement. Their distinct post-stop binding/transfer
contracts remain unchanged. External dynamic members are not proven absent.
Source compile and diffcheck passed. Raw check and source trace:
`/home/ts/.cache/agent-scratch/mendel-restart-admission-20261007/check.log`
and `source-family.json`. Parent owns integration/delivery; no old uncertain input
or completed public restart was repeated.

## Selected native rejection envelope — 2026-10-07

Original agent-comms-ux diagnostic:
`/var/tmp/agent-comms-live-20260927-wzjtqhza/diagnostics/4fbfe3ac651040b99a82452fe9ffeb50.json`.
Compaction preparation failed in PiRpcChannel -> PiEvent/Response normalization,
then SelectedPiProbeUnknownError. The raw rejected record was not retained. Its
exact envelope/native refusal and underlying send cause remain unknown; this
source correction does not reconstruct or replay that input.

The committed 4b RPC producer's error(id, command, message) emits exactly
id/type/command/success:false/error. Its selected preparation/settings/summary/
restoration branches and shared command catch use that original owner. Success
with a declared selected payload emits id/type/command/success:true/data.
Response previously accepted only the latter field set for all strict commands,
so a genuine native refusal was rejected before command correlation and its
reason could reach the existing diagnostic exception chain.

Response now validates those two exact original outcomes once while decoding
command: explicit boolean success, exact appropriate payload field set, string
native error. Mixed data/error, extra fields and null/nonboolean status still
refuse. Error responses retain existing MissingData; they grant no selected
payload, preparation, summary, session change, commit or replay authority.
Response.require_request still requires the exact original command/id and true
success before payload use, but now retains native refusal text in its existing
ValueError. Original _exchange_observation/_summary_exchange child retirement and
uncertainty handling remain unchanged. No new event, codec, response store,
diagnostic format field, native source/pin/package or authority was introduced.

Open Core719 touches ACP/evidence only, not this response family. No competing
response/native writer was identified. Original PiCommand strict declarations
(switch/session, summarize, settings, prepare, restore), PiPayload ingress,
NativeQuery correlation, selected observers/summary/restoration, pending-response
consumers and diagnostic rejection projection were traced. Existing Package
parsed/compiled all 759 src/tests/tools modules, zero omissions; 1046 related
before/after sites retained. Dynamic external subclasses remain unresolved.

13 boundary checks passed in the first batch. One new success fixture omitted
SessionSwitchData.cancelled and failed; its retained source requirement was
restored with cancelled:false, without production changes, and only that failed
check repeated/pass0.16s. All five strict commands decode the producer's exact
error envelope; malformed/mixed outcomes, wrong-id refusal, preserved native
reason, successful typed session outcome and original ordinary/opaque decoder
checks pass. These are JSON-line ingress checks, not a newly executed native
failure/provider/application qualification. Raw check.log/check02.log and original
diagnostic/producer source references: `/home/ts/.cache/agent-scratch/mre01`.
No provider/input/restart/public mutation/build/prefix operation or failed-input
replay occurred. Parent owns integration and future installed delivery; original
UNKNOWN input and diagnostic remain untouched.
