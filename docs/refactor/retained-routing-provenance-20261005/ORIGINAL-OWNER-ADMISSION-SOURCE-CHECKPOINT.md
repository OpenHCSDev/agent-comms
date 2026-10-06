# Original owner admission: implemented source checkpoint

This supersedes the implementation status of preserved proposal `ae01a146`.
The proposal and its original relation JSON remain historical bytes. Frozen
routing source paths, all thirteen helpers, `b52ed4`/`145cd9`, `c44`/`0fb` and
exact720 `c3e413` remain unchanged in their separate checkout.

The actual amended producer P is branch
`source/amended720-owner-admission-20261006`, commit
`bb5acfcd81b2fb0b2b7b2884c72e635a6880db5a`, based on genuine720
`72062939239b0f07707406309305a056a23f925f`.
It is new source, not exact720 or an original historical installed artifact.
P has no built wheel or installed acceptance. The current implementation is on
`implement/original-owner-admission-20261006`, based on published689 `1fd96070`.
Neither branch is described as latest-main or a frozen wheel equality.

## Actual resource declaration

Original720 has no StoreLock class. Its `_store_lock` yields an integer and
WireLog.locked passes that integer through. Those files and all their ordinary
consumers remain byte unchanged in P; their durability guard and last-close OFD
custody remain the authority. No current durability implementation is backported.

The existing OwnerLifecycle now owns `restart_wire() -> Iterator[int]`. P holds
the original integer acquisition. Current holds its native StoreLock resource and
explicitly extracts `lock.descriptor`. Both retain the acquisition context while
the phase owns an ExitStack. StoppedOwnerBatch stores only `wire_descriptor: int`.
Admission and completion acquire through that lifecycle method; target acceptance
retains the original fstat/device/inode/uid/regular-file/flock/root checks.
Subprocess pass_fds and recovery use that same integer. There is no object/int
fallback, adapter alias, second lock or new resource store.

| Fact | Owner and complete consumer change |
| --- | --- |
| Restart eligibility | Thread methods; current delegates native intent to its original ThreadExecution, P uses original role/status/process declarations. Request and resident queue call Thread. |
| Complete fence | Registration commits all RegistryDocument member fences in one editing transaction; P's per-owner committed wrapper is removed. |
| Exact launch capture | RetainedOwnerLaunch owns process, interpreter, route/incarnation, environment and arguments. P's queue removes its own environment class and `/proc` reader. |
| Retired witness | OwnerRestartSelection captures post-fence owner/admission generations and original process/incarnation. Its stopped validation never invokes the live-only snapshot attachment check. |
| Stopped status | Existing ThreadStatus/StoppedThreadStatus behavior; P adds require_stopped with no new wire field. |
| Handoff | RetiredOwnerLaunch stores selection plus launch. Later AdmissionIdentity is not required by this shared original-format phase; unrelated current native admission declarations remain. |
| Lifetime | Existing OwnerLifecycle remains sole signal/start owner; complete phases retain its stop guards, voluntary release validation and exact original lock. |
| Transport/recovery | Original installed P phase imports replace runtime module injection. All three subprocess command callers remove the package-path argument. Failed completion retains the same stopped batch; recovery still requires the operation's unchanged-original proof. |

Phase, launch and cutover declarations are byte equal in current and P.
P's Thread stored fields, RegistryDocument, FieldCodec, thread_identity,
store_files and WireLog are unchanged from original720. The original ordinary
`agent_bin="pi"` default is retained. The existing resident queue is migrated;
its obsolete external prompt queue, already calling absent old lifecycle APIs,
and those obsolete-API controls are removed from P.

`phased_owner_kernel.py` and all load_original_phase calls are deleted from the
current tools. No method is left behind an unused selector: ordinary restart,
queued restart, original admission, target completion and original recovery all
consume the changed contracts.

## Source checks and exact limits

The final authored in-memory batch passed three controls on current and three
on P: full stopped witness substitution refusals, strict codec/handoff roundtrip
with original launch values, and changed-original-incarnation refusal.
Receipt: `owner-admission-source-controls/results.json` (`d6ae8089`).
Changed source compiled with zero omissions. Original field and untouched-owner
relations were checked against Git720; the three shared phase modules are equal.

Preserved earlier source negatives: the old development interpreter lacked
metaclass_registry; P initially exposed absent snapshot.require and absent
ThreadStatus.require_stopped. The canonical mapping and original status family
now own those calls. No fallback was added. Raw tool transcripts and the receipt
retain those failures. OFD transport, atomic fence, signal/launch, stopped
installation and unchanged-source recovery controls remain UNRUN; these source
controls acquire no root, native package, child, provider or installed prefix.

## Missing live registry postimage

The actual old fact is Thread.last_goal_report_turn, read and written by original
GoalAction.apply to prevent repeated model reporting within a turn. P preserves
that field, writer and original bytes. Current Goal.reported_turn and history
report membership are current declarations, not an acquired mapping proof for
every old retained Thread and release receipt.

Reviewed existing candidates do not supply that mapping:

- NativeSchemaCarryPlan/CarryNativeRuntimeInstallation declare coordination,
  prompt-binding and compaction databases; goal-private store custody does not
  declare registry Thread fields.
- RoutingCarryPlan neutralizes only active_turn.routing and authenticates the
  remainder of the registry. It supplies no current Thread postimage.
- GoalReportMemberRetirement is an explicit one-shot member deletion, also used
  by an older live projection. Its deletion does not establish preservation of
  the goal-report fact at a current declared owner. It is unchanged and is not
  promoted to a valid carry by this checkpoint.
- RecordedRegistryProjection supplies read provenance only; it cannot become a
  live owner catalog or admission authority.

No existing preserving full-registry transition was established by this closure.
The remaining relationship is an original-declaration-acquired mapping of that
report fact and both registry/release carriers to actual current goal/history
owners, followed by strict target validation under stopped custody. That is
future source work and runtime qualification, not implemented or authorized by
the present task. A target still refuses old720 registry fields before launch.
This source checkpoint grants no native/schema/routing carry operation and no
central-batch acceptance. The separately issued stopped routing stages and proof
remain on their frozen source tuple and do not consume P.
