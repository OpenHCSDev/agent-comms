# Read-only live diagnosis, 2026-09-28

No provider sends, live mutations, or process stops performed by this worker.
Production source heads remain core cd034e6 / Toad 04fb611; published Toad
handoff head f48a32f65ba3fb43e3e1daae69abbf9b67076c2d remains unchanged.

## Fresh harness send misattributed to historical message

Original bus JSONL timestamps (America/Toronto):

| Sequence | Message ID | Timestamp |
| --- | --- | --- |
| 74 | 350cb7605e62 | 08:37:14.023102 |
| 76 | 8ca410057e58 | 08:46:03.710700 |

Both have identical `New independent installed Toad feedback check...` bodies.
Parent `installed_ui_live.py` lines 48-51 sends, then chooses the first
historical message with matching body. It never binds observation to the new
receipt identity. Parent `installed-ui-route-fixed.log` records SENDING and
SEND AND PAINT RETURNED; report elapsed 63.22 seconds and output files mtime
08:47:05 align with message76 at 08:46:03, although report names74.
This strongly attributes76 to that run, not a new worker probe. No persisted
writer PID is present in the bus record, so exact process attribution remains
an inference from these matching times and harness control flow.

Fix the harness by observing the newly committed exact sequence/message ID,
not the first body match. Parent owns the harness; no edits made here.

## UX pending has a concrete earlier execution blocker

Read-only coordination.sqlite3 query shows UX current_executions pointer:
`wirev14ce9bdc54dbdcbb7d1f680ab765b8eca8572f6ae3f4cc7d39ab6e7068cea95f3`,
attempt1, pointer_revision57. This is the message71 FULL execution.
Execution remains active; attempt remains prompt_starting, backend_done=0,
process_dead=0. Its lease expired at 1790598851395; no settlement occurred.
Its native input `2b5190de9b7e6e41ebe4f448209ba81d` has no persisted sent owner
admission epoch or native session proof.

Current `SelectedExecution._select` rejects a nonempty participant execution
pointer with `selected owner has an unresolved execution; new claims remain
pending`. `_uncertain_failure` only publishes the failure notice; it does not
settle the execution. Message74 and76 UX assignments remain triage_pending,
with no corresponding native input rows. This is a concrete admission blocker
independent of the previous UI root-resolution overhead.

Do not replay71 or infer successful/unsent outcome solely from absent proof.
Parent needs evidence-backed existing recovery/settlement semantics; this
read-only diagnosis has not changed durable outcomes or cleared the pointer.

## PR95 fresh execution succeeded

Message74 PR95 triage is deferred without admission/session proof.
Message76 PR95 triage input `1255d1e40202fe1fc84b67fbea34d828` has sent admission
epoch261 and native session `01a0e80d-369c-753b-b425-c0bb880ef08d`; its assignment
is ignored, triage_verdict=ignore. Thus route-fixed run's fresh message did
reach and complete PR95 triage, while its report watched the earlier UNKNOWN.

Parent retains live recovery, native diagnostics, DM recent-receipt UI and
installation ownership. No current evidence establishes a new persistent
route-lock deadlock after PF4.

## Worker import path and historical error timing

Channel75's generic UNKNOWN error was committed at 08:37:27.415171.
The cause-bearing native wrapper commit c74c374 is dated 08:39:29.
Installed native_pi.py mtime is 08:45:37.748632; current workers3753279 and
3753280 both started at 08:46:00. Thus channel75 predates both this fix and
these worker processes. It does not demonstrate that the restart loaded old
code. Parent restart_owners.json records previous workers3721670/3721673.

Both /proc cmdlines use runtime-acp-extensions-20260928/bin/python with
`-m agent_comms.worker`. /proc/exe resolves the shared uv interpreter, which
is normal for this venv. PATH=/usr/local/bin:/usr/bin, no PYTHONPATH or
PYTHONHOME; the explicit launcher selects the venv regardless of PATH.
Neither worker cwd has agent_comms.py, an agent_comms directory, or
sitecustomize/usercustomize. The venv excludes system site packages; its only
.pth is _virtualenv.pth, importing _virtualenv (no editable package path).

Read-only subprocess probes used each worker's exact interpreter, environment
and cwd with -B, resolving package/worker/native_pi via importlib.find_spec.
Both resolve all three to the installed runtime site-packages. sys.prefix is
the installed runtime; sys.path contains cwd, uv standard library paths and
that runtime site-packages only. No Comms construction, sends or worker
entrypoint execution occurred in these probes. They verify import resolution
under the same startup conditions, not an introspection of live sys.modules.

Message74's historical missing admission/session proof alone cannot identify
the precise underlying lock/exception: its old generic wrapper discarded the
cause from the published notice. Parent's newer diagnostic wrapper addresses
future evidence. Message76 already has successful current-worker PR95 proof.
