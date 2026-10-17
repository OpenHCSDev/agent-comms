# P181: commander parity — working checkpoint, not installation-ready

Read completely: RTS `order.py` (1–513), `judge.py` (1–171), `rotate.py` (1–21). Original scripts and JSONs were not changed or run. The replacement order is the 12:29 typed-field instruction, not the superseded prose-parser order.

## Rule census and ownership

**Implemented** means the fresh private Core CLI exercised the applicable workflow; it does not mean installed. **Missing** means the scratch implementation must remain. Tone and hedge word lists were deliberately deleted, as ordered; semantic judgment remains the author's job.

| RTS rule / source lines | Core owner / status |
|---|---|
| Required problem, source owner, check, edits, banned actions and report; order.py 70–108 | Typed `ProblemGoalOrder`, `goals.py:250`; `CommsTodoCreateTool`, `tools.py:597`. FieldCodec decodes once. No paragraph scraping. |
| Existing source file and positive in-range line; order.py 94–98, 172–174 | `FileLine.require_exists`, `change_report.py:34`; stops at the requested line. |
| Existing check directory, nonempty command, integer expected count; order.py 99–102 | `OrderCheck`, `goals.py:199`; filesystem validation occurs at admission, not when loading saved goals. |
| Numbered edits, Banned and Report clauses; order.py 80–85 | Required typed fields; `ProblemGoalOrder.instructions`, `goals.py:271`, derives the rendered instructions. |
| Bluntness, hedge detection, quotation exemptions; order.py 34–42, 67–79 | Intentionally no word-list enforcement. Required edits replace vague order shape; actual meaning requires review. |
| Recorded deadline; order.py 86–87, 444–450 | `EventTime.require_deadline`, `recorded_time.py:28`; absolute recorded time replaces HH:MM parsing. Overnight HH:MM conversion is not provided. |
| Problem born with owner and order identity; order.py 166–192 | `TodoState.require_owner`, `todos.py:94`; `TodoStore.create`, `todos.py:482`; `CreateProblemGoalAction`, `goal_actions.py:486`. Owner + generation + deadline are one SQLite row. Goal publication is a following guarded step; failures remain owned and appear as unpublished orders. No cross-file atomicity claim. |
| Unique problem identity and duplicate-create consistency; order.py 178–180 | Todo primary key and existing exact creation/assignment checks. Automatic P-number allocation is missing. |
| Problem description at least eight words; order.py 175–176 | Missing; current Todo text validation only enforces nonempty/bounded text. |
| One exclusive problem owner, no competing execution; order.py 103–114 | Existing assignment CAS; GoalOrder slot policy in `goals.py:213,264`, consumed by Thread. |
| Related-problem batching; order.py 57–59, 89–90 | Missing. Current tool creates one problem/order. |
| Concrete reassignment reason, tell old/new owner, replace exact prior message; order.py 108–114, 124–139 | Partial: assignment revision/generation checks and atomic second-strike handoff. General reassignment reason, notices and message replacement are missing. |
| Per-owner per-problem strikes; order.py 291–322 | `StrikeTodo`, `todos.py:191`; counter compares owner incarnation, not execution generation. |
| Two strikes take work away; order.py 118–119, 313–322 | `ReassignStrikeTodo`, `todos.py:211`: the second strike requires a different assignee in the same row update; new Goal published and old Goal cleared through existing actions. No unowned intermediate row. |
| Mechanical-only exception for fired owner; order.py 118–119 | Not provided; fired owner is refused outright. |
| Ten-word reason, explicit catcher/stage, public narrative and roast; order.py 288–322 | Missing. Reasons are retained but these fields/public messages are not implemented. No language blacklist was retained. |
| Done checks exact clean delivered commit and executes its declared count; order.py 325–345 | `DeliveredOrderGoalAction`, `goal_actions.py:346`, runs through BoundedRun/ProcessIdentity outside the wire lock, then rechecks the original Goal before publication. Git cleanliness is also checked after the command. |
| Delivery hash/PR/live-operation alternatives; order.py 329–331 | Hash delivery implemented. PR-number and live-operation variants missing. |
| Commit-derived delivery time, late delivery strike, met-deadline strike reset; order.py 346–376 | Missing. No claim that successful checks prove timeliness or a pushed commit. |
| Installed evidence before closure; order.py 195–210 | `InstalledProblemGoalOrder`, `goals.py:296`; `InstalledOrderGoalAction`, `goal_actions.py:381`; `CommsProblemCloseTool`, `tools.py:663`. Eight-word statement and existing artifact are recorded, not proof of the statement's truth. |
| No open orders at closure; order.py 201–203 | `DoneTodoState.prepare_change`, `todos.py:134`, uses existing GoalState completion policy; generic tool cannot skip it. |
| Fold duplicates without losing text; order.py 213–229 | Missing. |
| Drop only in favour of newer live order; order.py 151–163 | Missing. |
| Link older orders without conflicting owners; order.py 261–275 | Missing. |
| Non-order messaging and order-shape refusal; order.py 143–149 | Existing Comms send remains separate; no prose classification is performed. Semantic misuse is not mechanically prevented. |
| Sent-message records, author and original history; order.py 44–54 | Existing wire/Goal history owners remain; no SENT JSON side store introduced. Automatic order DM/notification is not implemented by this checkpoint. |
| Board derived from canonical facts; order.py 232–258 | `Goals.fleet_orders`, `goal_management.py:63`, exposes unowned, idle, overdue and unpublished records. Markdown board generation is not provided. |
| Missing first tool call after two minutes; order.py 362–381 | Missing. Native first-tool evidence has not been joined to these orders. |
| Unowned problem alarm after five minutes; order.py 459–461 | New open unowned rows are unrepresentable; query detects retained SQL anomalies without decoding them as valid Todo objects. No timer. |
| Idle-owner 20-minute clock; order.py 462–465 | Partial: existing AgentActivity reports currently idle owners. Exact 20-minute clock is missing. |
| Overdue orders; order.py 466–470 | Recorded deadline comparison in `EventTime` and fleet query; no timer thread. |
| Reply context before pushing overdue owner; order.py 421–441 | Missing. |
| Hunt file/line, ten-word finding, real order or clean result; order.py 389–398 | Missing. |
| Hunt candidate churn/least-recent selection and 15-minute alarm; order.py 401–418, 471–476 | Missing. No hunt side store was added. |
| Show complete unjudged edits beside current orders; judge.py 38–74 | Missing. Existing native edit facts were not given a review workflow in this checkpoint. |
| Per-edit read code after all hunks; judge.py 53–55, 107–110 | Missing. |
| Unknown edit refusal, exact quote for each edit, six-word reason; judge.py 99–117 | Missing. |
| Approval quotes four words of an actual order, including peer-order option; judge.py 77–90, 118–122 | Missing. |
| Send-back cites real commander message and 12-word public roast; judge.py 123–136 | Missing. |
| Persist verdict per edit, automatic strike on send-back; judge.py 130–139 | Missing. |
| Unjudged-five-minute alarm; judge.py 142–152 | Missing. |
| Next round-robin unit; rotate.py 8–12 | `SweepTodo`, `todos.py:237`, records an explicit inspection; fleet chooses least recently inspected open-order owner, alphabetical ties. Stable membership rotates round robin; mere reads do not advance it. |
| Orders, unjudged count and last turn on rotation; rotate.py 13–20 | Order identity/deadlines available. Unjudged count and last-turn display missing. |
| Commander last ordered/judged, quiet >2 minutes while orders open (new instruction) | Todo `commanded_at` and `swept_at` are separate facts. Fleet uses an indexed generated timestamp to find latest command, including closed problems. Sweep does not reset the work clock. True quiet alarm observed after actual elapsed time. Actual edit judgments remain missing. |

## Verification

- `private-cli-3.log`: actual fresh private bus/registry/Goal stores and CLI, no provider or public operations: required-field refusal, owned creation, fleet query, first strike, refusal of a bare second strike, atomic second-strike reassignment, old Goal cleared, overdue query, sweep, premature-close refusal, checked child-process delivery, evidence closure.
- `quiet-alarm.json`: later read of a separate retained private root showed a true two-minute commander alarm. No prior input was replayed.
- 18 existing Goal/mention tests passed. Existing old Todo primitive tests still need migration to the required owned-create API and removed release/transfer wrappers; they are not claimed passing.
- NRA before: 1083 modules, after: 1084, no parse omissions. Existing helper inventories declarations, writes, decisions, checks, imports, inheritance and consumers across Core, tests/tools, Toad, metaclass-registry and RTS source. Arbitrary callbacks/getattr, lazy registration and dynamic MRO are not proven by syntax.
- No installed acceptance, no merge, no runtime publication. Root-1 setup failed before tool invocation; root-2 create succeeded then query failed on a missing import; both retained. Root-3 completed the journey. No UNKNOWN provider/input was replayed.

## Budget and remaining delivery blockers

Net source is still over the +150 target. The extra blocks are not claimed as a budget grant:

- `goal_actions.py:346`: bounded external git/check execution, retirement and pre/post clean-commit checks implement order.py 325–345. Preparing outside wire custody plus captured-Goal recheck prevents a check from blocking the bus or completing a changed order.
- `goals.py:250`: typed required fields, rendered instructions and source/slot policy replace prose scraping. The Goal instruction consumers and mention digest consume the derived instructions, not a second stored order blob.
- `goals.py:296` / `goal_actions.py:381` / `tools.py:663`: installed-result state, artifact admission and closure guard implement order.py 195–210, separately from commit delivery.
- `todos.py:211`: atomic second-strike handoff, successor Goal publication and prior Goal retirement implement the two-strike rule without creating an unowned problem.
- `todos.py:237,458` / `goal_management.py:63`: explicit sweep, canonical command clock, generated indexed timestamp and quiet alarm implement rotate.py plus the two-minute commander requirement. There is no second ledger or timer.
- `tools.py:597,646,663,684`: catalog ingress exposes creation, checked delivery, installed closure and one fleet query to every harness; the source CLI exercised those actual declarations.

The removed assignment-transition metadata/release/transfer machinery no longer owns live reassignment. Existing durable rows need an explicit one-shot carry before installation; it is not implemented here. Original files must stay unchanged. Existing ordered Goals need typed source/check/edit fields supplied, not a prose-reading compatibility codec. Judge/hunt parity and the other Missing rows above remain unfinished. **No whole RTS script is deletable after installing this checkpoint.** Individual covered checks can be retired only when all their command's remaining rules have an accepted Core replacement.
