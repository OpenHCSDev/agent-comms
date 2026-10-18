# Remaining slop: plan for the commander

**Repositories:** agent-comms at `8a9062d5`, Toad at `7e78df69` (both 2026-10-18). **Scope:** every slop hotspot and structural debt found in the review of that date, as orders a commander can dispatch. The commander-native plan (`commander-native-plan.md`) covers the commander's own scripts and is not repeated here.

## Rules for every order

- Read first. Trace each fact to its owner, storage, lifecycle and consumers before changing anything. The counts in this plan only locate code; they never decide what is wrong.
- A lifecycle or an absence that changes behaviour becomes a declared family, with each member owning its behaviour and precedence carried by the hierarchy. Optional data that no consumer branches on stays optional.
- Delete what a change replaces in the same change. Report lines added and deleted.
- Toad changes are verified in the real application on real sessions.
- Plain words in every order, report and commit.

## Order 1: give agent-comms a layered package structure

**Why.** agent-comms has 300 top-level modules and eight subpackages. Which layer a module belongs to is a real fact that is stated nowhere, so every agent recomputes it from names and imports in every session. Agents miss existing owners and write duplicates, dead modules hide (two were found this week only through an import hook), and nothing can reject an import that crosses layers.

**Target layers.** The commander confirms the final mapping in step 1; the module name clusters already suggest it:

| Package | Owns | Current modules, by name |
|---|---|---|
| `wire` | the bus, records on it, delivery, checkpoints | `bus_*`, `wire_*`, `message_*`, `delivery_*`, `private_*` |
| `registry` | threads, channels, owners and registration | `registry_*`, `registration_*`, `owner_*`, `thread_*`, `channel_*` |
| `coordination` | goals, attempts, todos, relationships, wakes | `goal_*`, `attempt_*`, `coordination_*`, `wake_*`, and `coordination_tables` |
| `context` | turn context, working memory, instructions | `working_*`, `context_segments`, `instructions` |
| `turns` | runtime-independent turn handling | `turn_*`, `input_*`, `session_*`, `selected_*` |
| `runtime` | agent backends and harness bridges | `native_*`, `pi_*`, `acp_*`, `agent_*`, `runtime_*`, `_pi_helpers`, `cli_bridges` |
| `ui_model` | derived views for frontends (exists) | |
| `cli` | the command line and agent tools | `cli_*`, `tool_*`, `tools` |
| `commander` | the commander (exists) | |

**Dependency rule.** Each package may import only the packages listed before it, except that `ui_model`, `cli` and `commander` sit on top: they may import any lower layer, and nothing imports them.

**Steps.**
1. Write `docs/layout/MODULE_LAYERS.md`: every module with its package, derived from the import graph, and the list of imports that would violate the dependency rule. Fix or justify each violation before moving anything.
2. Move one package per change with a codemod that rewrites every import. No module changes behaviour in a move.
3. Give each package a short `README.md` saying what it owns and what it may import.
4. Add an import-layer check to CI that fails any forbidden import, and add the count of top-level modules to the ratchet so it can only fall.

**Deletes.** Every module that step 1 shows no layer depends on, after proving it unused with an import hook across the real entry points.

**Done when** no module except the package's entry points sits at the top level, the import-layer check passes on `main`, and each package has its README.

**Timing.** Step 1 now. The moves touch every import, so run them in a window with few changes in flight, before more modules are added to the flat directory.

## Order 2: Toad's conversation widget

**Why.** `widgets/conversation.py` has 97 `None` checks in 1,828 lines, more than twice any other Toad file, with one cause. The conversation's connection to its agent is an optional field: `self.agent` is checked for `None` 20 times, and `agent`, `_agent_data` and `_shell` another 16. That is a lifecycle without a type, so every method rediscovers which state it is in. The constructor's optional agent definition, session id, session key, title, initial prompt and transcript encode a second fact the same way: whether this is a new conversation, a resumed session or a restored transcript.

**Target.**
- An agent-connection family whose members are the states the lifecycle actually has. Trace them first; expect at least "no agent", "agent connected" and "shell attached". Each member answers what methods currently decide by checking for `None`.
- A conversation-start family (new conversation, resumed session, restored transcript), each member carrying exactly the values it needs, replacing the optional constructor arguments.

**Deletes.** The optional fields and the `None` checks they required, and any method that exists only to rediscover state.

**Done when** no method in the widget checks the agent, its data or the shell for `None`, the constructor has no optional start arguments, and the real application starts a new conversation, resumes a session, restores a transcript and attaches a shell correctly.

## Order 3: optional state in Toad's other widgets

**Why.** The next Toad files by `None` checks are `widgets/history_anchor.py` (33), `session_presentation.py` (36), `widgets/transcript_history.py` (32), `transcript_publication.py` (30), `widgets/thread_comms.py` (31) and `widgets/prompt.py` (27).

**Target.** For each optional field in those files, record one classification in the change: a lifecycle or absence that changes behaviour (make it a family, as in order 2), or optional data no consumer branches on (leave it).

**Done when** each file's classification is in its change, every lifecycle field is a family, and the real application's history, publication, thread and prompt behaviour is unchanged.

## Order 4: optional fields decoded at the Pi boundary

**Why.** `pi_payloads.py` projects Pi's wire payloads, including numeric fields the protocol may omit (token totals, request sizes, emitted event counts). Optional values are legitimate on the wire, but a consumer that branches on whether a value was reported is rediscovering a fact the decoder knew.

**Target.** For each optional payload field, find its consumers. Where any consumer branches on absence, decode the field once into "reported" and "not reported" members at the boundary. Leave fields nobody branches on.

**Done when** no consumer outside the payload module checks one of these fields for `None`.

## Order 5: classify the remaining agent-comms markers

**Why.** These files carry the next concentrations of `None` checks and string-keyed reads: `runtime.py` (20 and 15), `pi_events.py` (23 `None`, 5 string comparisons), `goal_attempts.py` (26 `None`), `cli_commands.py` (22 string-keyed reads), `acp.py` (3 attribute probes) and `native_package.py` (7 string comparisons).

**Target.** Read each and classify every marker: boundary decoding done once (leave it), a lifecycle or absence (make a family), string dispatch (replace with the owning family or a registry derived from declarations), or a probe (replace with the declared capability).

**Done when** every marker in these files is classified in its change and every non-boundary one is replaced.

## Order 6: one runtime path

**Why.** `native_pi.py` still exists beside the `AgentBackend` family, so Core talks to agents two ways. That is duplicate authority at the most important boundary in the system.

**Target.** Finish the Pi Durable cutover recorded in Toad's `DECISIONS.md`: every runtime operation goes through an `AgentBackend` member, and each cutover step deletes the Core code it replaces.

**Done when** `native_pi.py` is deleted and no module outside the backend family starts or drives an agent.

## Order 7: recheck two recent changes

**Why.** In the last review, two changes moved against the trend: the context-manifest change added 31 lines of `None` handling while deleting ten classes, and the drained-session record added 26. They may simply be optional fields moving to their new owners.

**Target.** Read the new optional fields. Convert any that encode a state to a family; leave genuine optional data.

**Done when** each new optional field is classified, and every state-like one is a family.

## Excluded on purpose

- **`field_codec.py`.** Most of its `isinstance` checks are on types themselves: it is the codec reading annotations to decode values once at the boundary. Inspecting types is its mechanism, so units must not "fix" it.
- **The commander's `judge.py` and private stores.** Covered by the commander-native plan.

## Order and dependencies

| Order | Depends on | Can run in parallel with |
|---|---|---|
| 1, step 1 (layer map) | none | all |
| 1, steps 2 to 4 (moves and check) | step 1; a quiet window | none, since moves touch every import |
| 2 (conversation widget) | none | 3, 4, 5, 7 |
| 3 (Toad optional state) | 2, for the pattern | 4, 5, 7 |
| 4 (Pi boundary) | none | 2, 3, 5, 7 |
| 5 (agent-comms markers) | none | 2, 3, 4, 7 |
| 6 (one runtime path) | the Pi Durable cutover | 2, 3 |
| 7 (recheck) | none | all |

## What each change reports

Lines added and deleted; for the files it touches, the `None` checks, string comparisons, probes and string-keyed reads before and after; for order 1, the number of top-level modules and forbidden imports. These numbers show progress. The classification in each change shows correctness.
