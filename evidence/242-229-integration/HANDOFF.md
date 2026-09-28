# Hotfix242 + parent229 + S12 integration

Ready for parent integration. This branch starts at parent a5809ab and merges
82f4c98 (hotfix242), 13547ee (S12), 29747fd (current catalog cutover caller and
parent checkpoint), and a194884 (hotfix merge/cleanup evidence). All four are
ancestors. Own tree: ~/wt/comms-242-integration-20260928. No parent, installed
runtime, live root, package or provider configuration was edited. PR245 targets
parent229's branch. Parent owns activation and merge-host configuration.

## Resolutions and caller closure

- compaction_journal.py: retained declared CompactionOperation and
  SelectedSummaryAttempt tables. New refusal, manual retirement and history
  inspection use existing typed select/one/update owners. No status columns,
  raw fetch/from_row/from_columns or second lifecycle loader were restored.
- compaction_states.py: retained polymorphic RefusedSummary,
  RetiredRefusalSummary and ManualCommittedSummary, serialized by FieldCodec.
  Manual commits cannot mint original-input admission; uncertain attempts remain
  blocking. Parent reservable_commit capability remains the reservation owner.
- manual_compaction_bridge.py: canonical marker selects the journaled manual
  path. Current active-turn exclusion and transcript start/end handling retained.
- owner_compaction_manual.py: current selected package identity is passed to
  existing selected settings and summary exchanges; NativeSummary owns typed
  SummaryFiles/SummaryUsage and the existing commit driver owns native writes.
- owner_compaction_commit.py: BoundedRun.run_inherited retains process/descriptor
  authority and deadline behavior; native helper receives the exact UTF-8 byte
  count. Old child-deadline/authority-launcher helpers stay deleted. Removed the
  obsolete total-summary cap; typed file paths and usage validation remain.
- pi_summary_payloads.py: preserve typed SummaryFiles and usage; remove obsolete
  aggregate file-metadata cap as well as count cap. Native model capacity and
  transport framing remain the existing owners.
- selected_pi_summary_rpc.py: current NativePiRpcLaunch.package check and
  PersistentPiSession process owner; new correlated increasing progress renews
  inactivity grace. Duplicate/foreign progress does not keep a stalled attempt
  alive. No total summary timer or old selected child watchdog was restored.
- wire_log.py: retain streamed delivery revision, canonical marker and current
  PublicationIntents owner. No deleted public writer or converter restored.
- Native helpers/manifest/pins and home package admission retain hotfix bytes.
- S12 source/callers retained intact, including NotificationAssignment,
  WakeAssignment.assignment_id and Thread.process_alive.
- current_root.py imports current ChannelCatalog directly; deleted
  tools/cutover/channel_catalog.py remains absent. --help imports successfully.
- Three conflicted tests plus new manual/retained fixtures migrated to current
  typed state, SummaryFiles/usage, AttachedChild, ProcessIdentity and package
  identities. Private history fixtures preserve prior STARTED evidence while
  removing only their unused synthetic original input. No production evidence
  was reset. Local scripted child waits for import readiness before transport
  inactivity assertions, avoiding a test of interpreter startup speed.

## Local acceptance

Python: parent worktree .venv/bin/python (interpreter only), PYTHONPATH=src from
this integration tree. pytest -q -o addopts='' disables parallel defaults.
Each command had a 35–55 second outer bound; temp data stayed in this tree.
Native package used read-only:
/home/ts/.local/share/agent-comms/native-compaction-policy-b3a9c06/node_modules/@earendil-works/pi-coding-agent
It passed this tree's native_package manifest verification.

- combined-guards.log: 63 passed, 2 native opt-in cases skipped. Includes selected
  transport/progress, typed journal, channel notifications and deletion guards.
- native-selected.log: both skipped native cases explicitly enabled and passed,
  alongside rejected stock package and inherited descriptor/parent identity:
  4 passed in 1.51s.
- manual.log: 5 actual native selected/manual/ACP cases passed in 17.67s.
  Includes known refusal retirement, UNKNOWN/reserved refusal to replay,
  native commit, transcript notification and no new original input.
- framing-commit.log: 7 passed in 31.17s. Actual native commit over the removed
  metadata/request caps, Unicode metadata, lost-result reconciliation and
  parent SIGKILL after stdin while inherited authority remains held.
- retained.log: copied actual 143686055-byte session, 312227 source tokens,
  seven loopback requests, native journal commit, strict reopen, 601 read files,
  211 modified files, idle owner, zero new user inputs. Original source size and
  mtime unchanged; no paid calls. 1 passed in 16.63s.
- final-guards.log: current combined tree's S9/S12/native/process/L0/S10 guards
  and notification checks. No broad full-suite claim.
- ratchet.json: relative to current parent29747fd, TypeIdentity -6,
  LongBooleanChain -3, StringSubscript -86. No metric increased.
- Ruff passed for hotfix production and migrated tests; git diff --check passed.

Earlier exploratory 55-second multi-file native run expired without a completed
suite result; it is not counted as passing. Bounded targeted shards above
resolved its fixture/caller failures. The initial channel transport run also
exposed the leftover metadata cap; it was deleted rather than raising a limit.
No waits on CI. These are local source/native tests, not a new live activation.
The user reports hotfix242 already installed and four owners live; deployment
of this combined refactor remains parent-owned.

## Size and remaining ownership

Combined source/tests versus parent29747fd: 63 files, 5317 added and 4057 deleted
before this evidence-only receipt (includes the full S12 integration and the
new manual behavior/acceptance). Net addition implements hotfix functionality
and S12 caller coverage while all three debt measures decrease.

Cicero owns the separate view_unread cancellation/scaling follow-through; it does
not hold this accepted S12 batch. T2 paired declared ACP boundary remains queued;
no Toad work was started here. No source converter or old codec was revived.
