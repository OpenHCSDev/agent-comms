# Pi Durable integration: an agent-backend layer for Core

This is a design only. I changed nothing in any repo. The pi-durable scratch directory has been removed; the backend-patterns directory is left for you. The other agent's uncommitted edits in the worktree (`compaction_private_inputs.py`, `continued_private_session.py` and tests) are theirs, not mine.

**Sources.** Core is `/home/ts/wt/comms-goal-ledger-schema-carry-20261002` at 51a78ace0. Pi Durable is `@earendil-works/pi-durable@1.1.0`, read from `dist/` in the packed tarball. I did not use the NRA tooling: no `nominal_refactor_advisor` is installed for `/usr/bin/python`. Instead I ran a purpose-built `ast` census over all 337 modules in `src/agent_comms`, and every module parsed. It is source evidence only; the dynamic dispatch through `DeclaredFamily.decode` is not resolved by it.

---

## 1. Census: where Core talks to the agent runtime

Tags: **D** means it exists because Pi 0.85.1 is not durable. **S** means Core does Pi's job from outside (the split owner described in forensics families A–F). **P** is a real product need that stays in Core.

| Fact / operation | What the AST found (counts, key sites) | Lines | Tag |
|---|---|---|---|
| **Spawning Pi and helper children** | Spawn calls: `child_process.py:599` (Popen) and `:963` (`create_subprocess_exec`). Launch: `native_pi.py:406` `NativePiRpcLaunch`, `native_custody.py:60` `PiSessionChild.start`. Four packaged helper programs: `context_tokens.py:24`, `native_fork.py:33`, `native_session_reopen.py:86`, `owner_compaction_prepare.py:163`. Commit-writer child: `native_compaction_writer.py:32`. Claimed-write child: `selected_tool_broker.py:180`. Core copies Pi's config locations at `owner_launch.py:61-72,104,112-114`. | process+RPC group: 10 modules, 4,647 | S, plus D for the helpers |
| **RPC commands** | `PiCommand` family has 23 classes (`pi_commands.py:85-543`), 6 of them custom `AgentComms*` (`:479-543`). 19 construction sites outside the family, for example `backend.py:711` Prompt, `:463` Abort, `turn_inputs.py:106` InterruptSteering, `config_options.py:147-190` catalog/model/thinking, `turn_stats.py:59,66` stats, `turn_runner.py:214` InspectContext, `native_custody.py:303` RestoreCompaction, `selected_pi_route.py:89,116`, `selected_pi_summary_rpc.py:105`. | (in the group above) | P for prompt/abort/model; S for the 6 custom commands |
| **RPC events** | `PiEvent` family has 41 classes (`pi_events.py`). 123 references from 23 modules; the largest are `turn_phase.py` 26, `tracked_turn.py` 20, `turn_runner.py` 12. Converted to Core's own `agent_events.AgentEvent` family (19 importers), which is already a backend-neutral event set. | turn orchestration: 14 modules, 4,246 | P |
| **Input identity and proof** | Native input IDs are added to Pi by patch (`patch-native-turn-context.py`, `native-request-input.mjs`). A proof journal is kept in `native-proof-journal.mjs` and `native_pi.py:184` `NativeContextJournal`. Also `native_runtime_input.py`, `native_input_record.py`, `native_prompt_binding.py:41` (`native_prompt_bindings.sqlite3`), input dispositions in `input_disposition.py:227` (26 importers), recorded contexts in `coordination.sqlite3` (35 path literals), the `failed_input_contexts` repair, and `historical_native_inputs`. | 17 modules, 3,922 | **D** |
| **Session files: reopen, fork, writer exclusivity** | `native_entries.py` decodes Pi's JSONL. Also `native_transcript`, `native_session_{files,reopen,prepare}`, `native_fork` (`fork_session.mjs`, a file copy), `fresh_private_session`, `session_fence`, and `native_custody` (empty, borrowed, retained, retiring and reopen states). `Thread.session_file` has 439 references in 72 modules. | 12 modules, 2,481 | **D**/S |
| **Compaction** | 17 `compaction_*` modules, 8 `owner_compaction_*`, `selected_pi_route`, `selected_pi_summary_rpc`, `selected_summary_admission`, `manual_compaction_bridge`, `retained_task_facts`, `context_tokens`, `native_compaction_{request,writer}`, `continued_private_session`, `proven_source_coverage`. `compaction-commits.sqlite3` is spelled at 12 sites. | 41 modules, 6,651 | **S** (forensics A–D) |
| **Budget / context usage** | Eight or more estimators. Python ones: `context_tokens.py`, `owner_compaction_settings.py:19-31`. JS ones: `native-context-budget.mjs:14-20,132-168` (including a regex retry on provider errors), `native-session-storage.patch:1858,1880,1883,2061,2283`. | stack | **S** |
| **Steering ("Send now")** | `turn_inputs.py:106` sends `InterruptSteering`, implemented by `patch-native-steering.py`. | — | P |
| **Tools** | The coding-tool admission hook (`channel_coding_tools.mjs`) calls Core over a socket, using `AGENT_COMMS_SELECTED_TOOL_SOCKET`/`_TOKEN`. Comms tools: `extensions/pi-agent-comms/index.ts` runs `agent-comms tools` and `agent-comms invoke` through a CLI child process for every call. Also `native_tools`, `native_tool_call`, `selected_tool_broker`. | 7 modules, 1,534 | P (S for the transport) |
| **Environment variables** | 31 names, 112 sites. Runtime-coupling ones: `AGENT_COMMS_NATIVE_CONFIG_DIR` (it also acts as the "Core-managed" switch, `native-session-context.mjs:73`), `PI_CODING_AGENT_DIR`, `AGENT_COMMS_AGENT_BIN`/`_ARGS`, `AGENT_COMMS_SELECTED_TOOL_*`, `PI_OFFLINE`, `AGENT_COMMS_SELECTED_SOURCE_COPY`, `AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE`. The `PI_AGENT_ID`, `PI_PARENT_ID`, `PI_TASK` group is read by the comms extension. | — | S |
| **Pi patches** | 13 patch files and 15 `native-*.mjs` files, about 7,600 lines; `native-session-storage.patch` alone is 3,222. | stack | D/S |

**Total.** About 23,500 Python lines across 101 modules sit on the runtime boundary, plus about 7,600 lines of patches. Roughly 13,000 Python lines (input proof, session files, compaction) and almost all of the patches exist only because Pi 0.85.1 is neither durable nor the single owner of its own compaction.

---

## 2. Mapping to Pi Durable

All paths below are in `@earendil-works/pi-durable@1.1.0/dist`.

| Core fact / operation | Pi Durable primitive | Effect |
|---|---|---|
| Native input ID, "input committed", UNKNOWN sends | `submit({requestId})` is idempotent: a known request ID returns the existing submission (`harness/submissions.js:117-127`). `harness.submission(id)` finds it again after a restart. | Core's input ID becomes the `requestId`. Re-sending a submit after a crash is idempotent admission, not a replay. The UNKNOWN-send states go away. |
| Input disposition (started / settled / not sent) | Submission status: `queued`, `placed`, `done`, or `unanswered` with a reason (`aborted`, `stale`, …). | Core keeps only the delivery link: which bus messages went into which input ID. |
| Recorded context per input (proof journal) | Entries are immutable. `Conversation.context({at})` returns the model context as of any visible entry (CHANGELOG 1.1.0). | Delete. The transcript is the record. |
| Compaction: trigger, summary, commit, reload | One engine, `pi.compaction` (`harness/compaction.js:54`). Blocking threshold, background threshold and overflow are decided in `generation.js:213-219`. On a context-overflow error it compacts once and retries (`:346-364`). The summary is placed through an idempotent write (`compaction.js:354-367`). A summary whose cut reaches before the current context settles as `stale` (`inbox.js:73`). | This removes forensics families A–C. The staleness check is one comparison of entry IDs, not the whole-world fence of family B. |
| Budget | `estimateContext` (`compaction.js:257`): the newest assistant usage plus estimates of the messages after it. `selectCut` uses `keepRecentTokens`. | One owner. **Gap:** `estimateContext` is not in the package `exports` map. |
| Steer / follow-up | `whenBusy: "steer" \| "followUp" \| "reject"`. Inbox placement happens at boundaries (`inbox.js:34`). | Maps directly. |
| Send now (interrupt the current tool round) | None. | **Gap.** |
| Abort | `conversation.abort()` withdraws queued inputs; `harness.abortTask(id)`. | Maps. |
| Fork | `conversation.fork(entryId)` within one store; the fork gets a fresh provider session ID. | Same-store only. See section 4. |
| Crash recovery | `Harness.open` turns `running` tasks back to `pending` and dispatches nothing (`scheduler.js:87-111`). `inspect()` is read-only (`harness.js:152`). Tools rerun only if declared `replay: "safe"`; otherwise the model gets an "interrupted" result (`tool.js:73-86`). An interrupted **model request is re-sent** when scheduling resumes. Aborting a generation sends nothing new (`generation.js:168-182`). | Core must decide what to do with interrupted requests before calling `resume()`. |
| Usage / cost | `pi.usage` document per conversation (`harness/usage.js`). | Turn charges become deltas between commits. |
| UI state | `watch()` and `viewState()`; `watchEvents()` gives a snapshot plus per-commit batches, with a fresh snapshot after more than 100 batches of lag. | The worker converts these to `AgentEvent`. |
| Model and thinking level | `configure({model, thinkingLevel})`, stored in `pi.agent`. | Maps. |
| Comms and coding tools | `defineExtension({tools, hooks})`; `beforeTool` can block a call or rewrite its arguments. | The comms catalog becomes an extension in the host process; claim admission becomes a `beforeTool` hook. |
| Goal continuation | `GenerationTask.onYield` can continue the run with another message. | Core's goal policy decides; the hook asks. |

**Gaps: things Core needs that Pi Durable does not have.**

1. Channel routing annotations, delivery dispositions, triage, goal budgets and the ACP surface. These are Core's own facts and stay in Core, keyed by the input ID / `requestId`. Pi Durable never needs to see them.
2. Single process per store, with no cross-process locking (README "Storage"). Core must enforce one owner (section 4).
3. Context usage is not public: `estimateContext` is internal. Re-implementing it in Core would bring back family A. Request the export upstream; until then the host can report it only by importing the non-exported file, which is a pinned-version risk.
4. No "Send now". Abort, then place the queued steer: there is no public "place queued items now" call.
5. No `AGENTS.md`, `APPEND_SYSTEM.md`, skills or prompt templates. The coding-agent loaded these; Core's host has to supply them as sections.
6. Tool parity. Pi Durable ships `read`, `write`, `edit`, `bash` and `powershell`, and cannot read images. The `pi-mcp-client`, `pi-web-search`, `pi-subagent` and `pi-project-sync` extensions are written against the coding-agent API and need porting. `pi-subagent` is replaced by task-owned conversations (README example 23).
7. Summary cost. The summary is one flat-text request with `cacheRetention: "none"` and tool results cut to 2,000 characters (`compaction.js:11,95-136`). There is no chunking, so a prefix larger than the model's window cannot be summarized.
8. Re-sending interrupted requests by default conflicts with your rule against replaying an interrupted attempt (gate in section 4).

---

## 3. The interface: an `AgentBackend` family

This uses Core's existing `DeclaredFamily` (`declared_family.py`, built on `metaclass_registry.AutoRegisterMeta`) rather than a new mechanism. It follows polystore: the registry key is a declared class attribute, capability role classes are composed through the MRO, and an opened instance holds the resource. It follows arraybridge: one pivot (Core's record types), each backend converts to and from it, and the registry is checked for completeness and frozen at import.

```python
# agent_backend.py
class AgentBackend(DeclaredFamily, affix="Backend"):
    """One agent runtime, keyed by declared_name ("pi_native", "pi_durable").

    A thread stores (declared_name, location). The location type belongs to the
    backend, so no consumer reads session files or stores. Opening a backend
    is an async context manager that owns the runtime process for one thread."""
    location_type: ClassVar[type["RuntimeLocation"]]
    serves_threads: ClassVar[bool] = True

    def __init__(self, location: "RuntimeLocation", thread: "Thread") -> None: ...
    async def __aenter__(self) -> Self: ...   # launch / attach, take the store lock
    async def __aexit__(self, *exc) -> None: ...  # retire the process

class RuntimeLocation(ABC):
    """Where one thread's runtime state lives. Replaces Thread.session_file."""

# Capability roles (mixins)
class RunsTurns(ABC):
    """Durably accept input under Core's InputId and report its outcome."""
    async def submit(self, request: "InputRequest") -> "InputOutcome": ...
    async def outcome(self, input_id: "InputId") -> "InputOutcome": ...
    def events(self) -> AsyncIterator["AgentEvent"]: ...  # snapshot first, then deltas
    async def abort(self) -> None: ...

class Steers(RunsTurns):
    """Accepts WhenBusy.STEER and FOLLOW_UP while a run is going."""

class Interrupts(Steers):
    """Ends the current tool round and places the named queued steers now."""
    async def send_now(self, input_ids: tuple["InputId", ...]) -> bool: ...

class Compacts(ABC):
    """The backend owns when and how to compact. Core only asks for a manual one."""
    async def compact(self, instructions: str | None) -> "CompactionOutcome": ...

class ReportsContext(ABC):
    """Context size measured by the backend's own estimator. Core never estimates."""
    async def context_usage(self) -> "ContextUsage": ...
    async def usage(self) -> "UsageTotals": ...

class ReadsHistory(ABC):
    """Pages of the transcript, newest first, for lazy history."""
    async def entries(self, before: "EntryRef | None", limit: int) -> "EntryPage": ...

class Forks(ABC):
    async def fork(self, at: "EntryRef", child: "ThreadName") -> "RuntimeLocation": ...

class RecoversAfterCrash(ABC):
    """Lists interrupted work before anything is resumed. Nothing runs until settled."""
    async def interrupted(self) -> tuple["InterruptedWork", ...]: ...
    async def settle(self, work: "InterruptedWork", disposition: "Disposition") -> None: ...
    async def resume(self) -> None: ...

class ConfiguresModel(ABC):
    async def models(self) -> tuple["ModelRef", ...]: ...
    async def configure(self, model: "ModelRef | None", thinking: "ThinkingLevel | None") -> None: ...

class ImportsHistory(ABC):
    """Writes another backend's transcript into a new location of this backend."""
    @classmethod
    async def import_from(cls, source: ReadsHistory, location: "RuntimeLocation") -> "ImportResult": ...

THREAD_CAPABILITIES = (Steers, Compacts, ReportsContext, ReadsHistory,
                       RecoversAfterCrash, ConfiguresModel)
```

**Pivot records.** Core owns these, every backend converts to them, and no consumer sees Pi types.

```python
@dataclass(frozen=True) class InputId: value: UUID           # minted by Core; it is Pi Durable's requestId
class WhenBusy(Enum): FOLLOW_UP, STEER, REJECT
@dataclass(frozen=True) class InputRequest:
    input_id: InputId; content: tuple[ContentBlock, ...]; when_busy: WhenBusy
    # routing / goal / bus-delivery provenance stays in Core, keyed by input_id

class InputOutcome(DeclaredFamily, affix="Input"):        # Queued, Placed(entry), Answered(entry),
    ...                                                   # Unanswered(reason)
class UnansweredReason(DeclaredFamily, affix="Reason"):   # Aborted, Stale, Failed(message), NoModel
@dataclass(frozen=True) class EntryRef: order: int; backend_id: str
class TranscriptKind(DeclaredFamily, affix="Entry"):      # User, Assistant, ToolResult, Compaction,
    ...                                                   # Reset, System
@dataclass(frozen=True) class TranscriptEntry: ref; kind; messages; usage; at
@dataclass(frozen=True) class ContextUsage: tokens: int; window: int; reserve: int
class CompactionOutcome(DeclaredFamily, affix="Compaction"):  # Placed(summary, first_kept, reason),
    ...                                                       # NothingTo, Failed(message), Stale
class InterruptedWork(DeclaredFamily):                    # InterruptedRequest(inputs, attempt),
    ...                                                   # InterruptedTool(call, replay_safe)
class Disposition(Enum): RESUME, ABANDON
```

Live events keep using the existing `agent_events.AgentEvent` family. It is already the pivot that 19 consumers read, so no second event set is added.

**Conversion and completeness.**

- Each backend declares one converter class per source event kind, in a family keyed by `source_type`. Pi-native already has this through `PiEvent`. Pi Durable gets a `DurableEvent` family keyed by `message_start`, `tool_execution_start`, `compaction_end`, `snapshot` and so on.
- At import, `_validate_backends()` (the counterpart of arraybridge's `_validate_registry`) checks:
  1. Every backend with `serves_threads` is a subclass of every role in `THREAD_CAPABILITIES`. The ABCs already enforce the methods.
  2. Each converter family's keys exactly equal the event kinds the pinned host protocol lists. The host's `protocol.json` is generated at build time from the 1.1.0 `AgentEvent` union, so a version bump that adds a kind fails at import instead of being dropped.
  3. Each `location_type` belongs to exactly one backend.
- After validation the registries are frozen with `MappingProxyType`.

**What each backend absorbs or deletes.**

- **`PiNativeBackend`** (Pi 0.85.1, transitional). It absorbs `backend.py` (`PersistentPiSession`, `TurnSession`), `native_custody`, `native_pi` launch, `pi_rpc`, `pi_commands`, `pi_events`, `pi_payloads` and `pi_vocabulary` (as its private codec), `native_session_*`, `native_fork`, `native_entries`/`native_transcript` (as `ReadsHistory`), `turn_stats` (as `ReportsContext`, using `GetSessionStats`, Pi's own number), and `turn_inputs`/`InterruptSteering` (as `Interrupts`).
  - **Compaction collapses into Pi's in-process engine.** `compact()` sends Pi's own `Compact` RPC (`pi_commands.py:442`) with `customInstructions`, and Pi's threshold and overflow compaction stay on.
  - Deleted from Core: the 8 `owner_compaction_*` modules; the `compaction_*` journal, record, state, source, boundary, operation, publication and send-gate modules; `selected_pi_route`, `selected_pi_summary_rpc`, `selected_summary_admission`, `native_compaction_{request,writer}`, `continued_private_session`, `proven_source_coverage`, `failed_input_contexts`, `context_tokens`; and the `compaction-commits.sqlite3` store.
  - Deleted from the stack: `native-compaction-{selected-summary,source,policy,commit-child}.mjs`, `native-session-context.mjs`, `native-context-budget.mjs` (its regex retry), `inspect-native-compaction-plan.mjs`, `_pi_helpers/prepare_compaction.mjs` and `context_tokens.mjs`, `patch-native-{selected-compaction-summary,adaptive-settings,context-budget}.py`, `native-summary-prefix.patch`, the compaction half of `native-session-storage.patch`, the 4 custom compaction RPCs and `AgentCommsRestoreCompaction`, and the `AGENT_COMMS_NATIVE_CONFIG_DIR` switch.
  - Kept: Pi's own chunking patch, if Pi's engine still needs it for long prefixes.
- **`PiDurableBackend`** is `pi_durable_backend.py` plus a Core-owned `pi_durable_host.mjs`, the only file that imports Pi Durable. When the last thread has been cut over, everything `PiNativeBackend` absorbed is deleted outright, along with:
  - the input-proof group (17 modules, 3,922 lines; `input_disposition` and `input_attempt` shrink to bus-delivery provenance);
  - `session_fence` and the remaining session-file modules;
  - all 13 patch files, the 15 `native-*.mjs` files, `pi-native.sha256` and the import-fence and package-pinning machinery;
  - `channel_coding_tools.mjs` and the selected-tool socket;
  - `extensions/pi-agent-comms` (replaced by an extension in the host);
  - `Thread.session_file`, migrated in its 72 consumer modules to `Thread.runtime: RuntimeLocation`.

**Edit count for a new case.** Adding a backend today means changing roughly 72 modules that read `session_file`, plus the RPC vocabulary. With this interface it is one subclass with its converters; the validator lists anything missing.

---

## 4. Process model

- **One host process and one store per thread.** The worker launches `node pi_durable_host.mjs` through the existing `child_process` owner. The store is `<root>/runtimes/<thread>/durable.sqlite` (SQLite in WAL mode).
  - The host takes an exclusive `flock` on `<store>.lock` and exits if it cannot. That lock is the single writer, and its holder is the one fact recorded in `owner_lifecycle`.
  - Memory is comparable to today's retained Pi child. The README puts TypeBox at about 23 MB RSS unbundled.
  - One crash affects one thread.
- **Protocol.** JSON lines over stdio. Core owns the protocol, versions it in lockstep, and decodes it once in Python.
  - Requests from the worker: `open`, `inspect`, `settle`, `resume`, `submit`, `abort`, `abort_task`, `compact`, `configure`, `models`, `entries`, `context_usage`, `fork_export`, `import_batch`, `close`.
  - Pushes from the host: `events` (from `watchEvents`: snapshot first, then batches) and `tool_call` / `before_tool` callbacks to the worker.
  - These callbacks replace both the `agent-comms invoke` child per tool call and the selected-tool socket. The comms tool catalog is registered as one `defineExtension` built from Core's catalog.
  - Comms tools keep the default `replay: "unsafe"`, so after a crash the model gets an "interrupted" result instead of a second send.
  - Errors cross the boundary typed and carry Pi's message unchanged (family E).
- **Startup gate.** `Harness.open` → `inspect()` → `RecoversAfterCrash.interrupted()`. Core records each interrupted generation or tool and applies the disposition: the default is ABANDON, which runs `abortTask`, records the partial answer as aborted, and makes no provider call. Only then does it call `resume()`.
  - Submit, wait and compact also start scheduling (`types.d.ts:510`), so the worker sends nothing until the gate has finished.
- **Readers: Toad, ACP and the CLI.**
  - Toad keeps talking ACP to the worker. The worker passes on the snapshot and converted events, and serves history pages through `entries` (newest first, cursor-based), which fits the lazy-history viewport.
  - Nothing outside the host opens the store. `transcripts.py` (35 `session_file` uses), `transcript_routes`, `compaction-status` and the other CLI readers move to asking the owner over the existing runtime attachment (`runtime.py`).
  - For a thread with no worker running, `owner_lifecycle` starts the host in view mode: open and read, never `resume()`, under the same lock.
- **Forks.** A fork in Pi Durable stays inside one store. Across per-thread stores, `fork_export(at)` returns `context({at})`, the active context at that entry, which is bounded by the model's window. The child's host imports it as a compaction head plus the kept entries; Core records the parent location and entry. This replaces copying session files.

---

## 5. Migration plan

Each step deletes what it replaces. CI is deferred, so every acceptance check runs locally and on the installed path.

**(a) Interface plus `PiNativeBackend`, with one compaction engine.**
- Work:
  - Add `agent_backend.py` and the validator.
  - Move today's behavior behind the roles.
  - Migrate the 72 `session_file` consumer modules to `Thread.runtime`.
  - Delete Core's compaction engine, journal, external writer and reload (list in section 3).
  - Turn Pi's own threshold and overflow compaction back on.
  - Pass Core's task brief as `customInstructions`.
- Acceptance:
  - An AST deletion gate in the style of #60 shows `compaction_journal`, `owner_compaction_*`, `external_write`, the `AgentComms*Compaction` RPCs and `AGENT_COMMS_NATIVE_CONFIG_DIR` have no definitions or consumers left.
  - A count of context-size estimators reachable from Core finds one.
  - Live: a long saved session (forked copy) compacts on threshold and on overflow, and the next reply works without reopening the child.
  - A manual `/compact` works from Toad.
  - An inbox blocked by compaction can no longer happen; the journal that blocked it is deleted.

**(b) `PiDurableBackend` for new threads, measured.**
- Work:
  - Pin `@earendil-works/pi-durable@1.1.0`, `@earendil-works/pi-ai@1.1.0` and `@earendil-works/chord@1.1.0` exactly, with lockfile integrity.
  - Write the host and the comms-tool extension.
  - Supply `AGENTS.md`, `APPEND_SYSTEM.md` and skills as sections.
  - Port MCP and web-search.
  - New threads get `pi_durable`.
  - No Core journals are added.
- Measurements, taken on the real comms journey (fork, open the tab before the first answer, first message, channel/DM reply, queue, status, history):
  1. Reply latency for a cold host and a warm host, and first-token latency against pi-native on the same provider and model.
  2. Compaction correctness: background, blocking and overflow each place a summary; a stale summary is discarded; the conversation keeps working during the summary.
  3. Crash recovery: `kill -9` the host during streaming, during a tool and during a summary. Then confirm no provider call is made without a recorded disposition, submissions settle, and the transcript matches the committed state.
  4. Host memory.

**(c) Import and per-thread cutover.**
- Work:
  - `PiDurableBackend.import_from(PiNativeBackend)` reads the active branch through the existing `native_entries` decoder, so the Pi 0.85.1 format is still decoded only once.
  - It appends every entry, and adds each Pi compaction as a `pi.compaction` head pointing at its first kept entry. The newest head determines what the model sees.
  - The thread switches backend under the owner lock. The old JSONL is archived read-only.
- Acceptance:
  - For each imported thread, the messages of `context()` equal the messages of Pi 0.85.1's built session context, compared message by message.
  - Context usage matches within the estimator's tolerance.
  - Toad pages the full history and End lands on the bottom.
  - The first reply after cutover succeeds.
  - Cut over one thread at a time, starting with forked copies of your saved sessions.

**(d) Deletion.** After the last cutover, delete `PiNativeBackend` and everything it absorbed, all patches and native `.mjs` files, the `pi-agent-comms` extension, the runtime environment variables and the importer. The AST gate proves that the `PiCommand`, `PiEvent` and `NativeContextJournal` families and `session_file` have zero consumers.

**Risks.**
- **Experimental API.** There were 14 releases in 19 days, and 1.1.0 broke the `Storage` and `runtime.context` signatures. Mitigations: exact pins, Pi Durable imported in one host file only, a protocol listing generated at build time and checked by the import-time validator, and an upgrade treated as a deliberate change.
- **Provider parity.** pi-ai 1.1.0 has `openrouter`, `openai-codex`, `anthropic` and others. OAuth and `auth.json` wiring must be checked live on your configured provider.
- **Tool and steering parity.** No Send-now, no image reading, and the extensions need porting.
- **Cost.**
  - Background compaction starts 32,768 tokens before the blocking threshold.
  - Summaries do not use the prompt cache.
  - A resumed interrupted request would be paid twice, which the startup gate prevents.

---

## 6. Open questions for Tristan

Each has a recommended default that I will use unless you say otherwise.

1. **Store granularity.** Per-thread host and store, which matches today's one-worker-per-thread model, or one store per project, which gives native forks, subagents and a task graph across threads but a single point of failure? *Default: per thread.*
2. **Interrupted model request after a crash.** *Default: abandon (abort the task, keep the partial answer as aborted), show it in the thread, and never resend automatically.*
3. **Task-aware summaries.** *Default: drop Core's selected-summary engine and pass Core's task brief as compaction instructions. A `beforeCompact` hook that supplies its own summary would be a second engine, so no.*
4. **Pi session-tree branches that are not on the active path.** *Default: import the active branch only and keep the JSONL archived read-only indefinitely.*
5. **Send now.** *Default: request a "place queued now" API upstream and leave Send-now out of durable threads until it lands. Resubmitting under a second request ID would map one input to two submissions.*
6. **Upstream versus local changes.** *Default: no patches to Pi Durable. File upstream issues for exporting `estimateContext`, for Send-now placement, and for chunked or cached summaries.*
7. **Background compaction.** *Default: on, with Pi's defaults (`reserveTokens` 16384, `keepRecentTokens` 20000, `backgroundTokens` 32768).*
8. **Model and provider for step (b) measurements.** *Default: your configured provider and model only, under the standing authorization. Nothing else paid without asking.*
