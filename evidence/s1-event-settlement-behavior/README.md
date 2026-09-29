# S1 current event and terminal-settlement behavioral acceptance

Owner scope: tests/test_s1_event_behavior.py only; no backend303, production,
shared launcher, live state or provider account edits. Current main420476a7 includes
Boyle301 nominal store deletion and300 legality fixture caller migration.

## Historical oracle and current binding contract

Original f9854ab: acp.py relay settlement895–914, event effects2670–2840,
terminal publication/finally3050–3076; manual_compaction_bridge.py finally; and
operations.py pause_waits_after_terminal_turn2438 onward. Read via git only; no
old module, dictionaries, transition tables, format decoder or consumer restored.

- Activity: tool start WORKING/title; tool finish THINKING/task. Compaction holds
  WORKING/Compacting context while recording a pending tool activity, invalidates
  context usage and restores the activity after end/abort. New declared cases
  consume through existing TurnProgress/MRO, not a compatibility event adapter.
- Metadata: AgentInfo records model/session/context; current thread/config owners
  observe it. Model/thinking results resolve separate family-correlated futures,
  including equal request IDs, failed result and a late result after completion.
- Output: successful chunks become one durable reply. Interrupted provisional
  text clears. Failed provisional text never becomes a successful wire message;
  diagnostic/reason errors retain structured terminal evidence and a non-waking
  diagnostic link. Current native503 path makes one provider request/one input
  start, never auto resends.
- Goals/settlement: input start, tool result, Done and StreamSettled synchronize
  goal observation. Native StreamSettled finishes the exact lease and publishes
  the stream while waits remain, until Done and durable terminal reply/failure
  are published. Shared settlement then releases a real dependency wait. The
  transport-error path releases after attempted terminal publication, once,
  without erasing a newer turn. Existing actual standby fence/reopen tests remain.
- Recovery/error: error-report transport failure does not cause duplicate error
  publication or replace original error text. Structured diagnostic and reason-code
  failure cases plus actual native503 cover the current reachable error boundary.

## OPEN2 disposition and deliberate policy delta

The old manual bridge omitted pause_waits_after_terminal_turn despite owning a
real dependency-visible turn lease. That omission is closed by the current shared
settle_turn owner: manual success, manual safe-cut refusal, relay and native turns
all commit/attempt terminal publication, finish the exact lease and release waits.
No artificial exclusion parameter or special manual hold exists.

The old operations.py implementation PAUSED the dependent goal when no qualifying
reply arrived. Current owner-authorized policy deliberately clears standby while
retaining an ACTIVE goal and an explanation; it never admits/replays input during
settlement. Tests assert that current behavior. No claim of old paused-state
identity equivalence. A qualifying direct reply, owner pause, newer incarnation/
turn and stale callback remain governed by current GoalWait/fence checks.

## Actual evidence and precise closure

- installed-complete.log:52 passed59.04s on main103a9dc9 noneditable core, including
  new current consumer cases, actual pinned native ordinary success/provider503,
  two real manual safe-cut refusals, plus existing event, real child settlement
  and actual standby fence/reopen/correlation checks.
- relay-installed.log:1 passed2.02s through actual prompt_owned relay entrypoint,
  bus send/inbox drain/terminal publication and real dependency wait release.
- manual-summary-commit.log:1 passed20.99s: three real native historical inputs,
  one summary provider request, actual manual bridge/journal/native compaction
  commit and dependency wait release. No original input replay/admission.
- current-installed.log:16 passed65.04s, final rerun on main420476a7 with strengthened
  manual assertions (success/refusal outcome, exact provider request count,
  exactly one saved compaction entry and preserved user input count).
- Native bundle remains immutable5fde; only isolated project/config/history roots
  and a loopback provider are used. Publication observation wrappers always call
  the actual methods; no mocked store, goal update or settlement implementation.
- Earlier red/partial receipts retained: first missing explicit private root/package
  configuration; second wrong tool sync count/reinitializing an initialized root;
  installed-final missing fixture reexport removed by lint. They are not green.
- Small-history preparation correctly declined no complete safe cut. Earlier
  boolean-only manual checks prove refusal/release, not summary success. Updated
  fixture supplies three complete exchanges large enough for a real retained cut;
  it does not alter production budgets, settings rules or native source.

Closes current S1 reachable event/recovery/terminal effects and all three settlement
paths, including OPEN2. Original before/after historical stream replay through the
now-deleted agent_loop consumer remains superseded/unproved; no exhaustive historic
oracle equality, full-suite, universal size/performance or certified NRA coverage
claim. Existing live S4 paint path stays with parent/Carver. No obsolete S1 fixture
was needed or restored; existing new-case/correlation/fence tests remain meaningful.
