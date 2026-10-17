# P181: typed order, create, delivery and closure

Scope replaced by Claude's 12:53/12:55 orders. Fleet rotation, commander quiet alarms and automatic second-strike handoff are removed, including their operations, query, clocks, generated column and index. Judge/hunt parity is not implemented. This is not a complete replacement for RTS.

The Goal action owns the typed order and its instructions. Creation and assignment share `ProblemOrderGoalAction`; only assignment carries a revision to compare. `CommsTodoCreateTool` specializes `CommsOrderTool`, so the tool does not redeclare and remap all the order fields. `TodoStore.create` requires an assignee, generation and deadline and writes the assignment at creation. There is no second constructor-level ownership rule when reading existing Todos.

`DeliveredOrderGoalAction` uses the existing BoundedRun owner. One clean-commit check precedes the command. A check must print nothing and finish with `ExitedOutcome(0)`; there is no expected field, stdout count extraction or regex. The Goal captured before the external command is compared again before publication. Failed or timed-out commands do not complete the Goal.

Closure still requires a completed order, an eight-word works statement and an existing artifact file. The action records the evidence on the Goal, and the Todo completion policy checks that evidence. No installed acceptance is claimed by these structural checks.

The prior owner's before census covers 1084 Core, Toad, metaclass-registry, test/tool and RTS modules without parse omissions; retained at `/home/ts/.cache/agent-scratch/core-commander-2-p181/final-ast.txt`. Dynamic family registration, MRO and callbacks require semantic reading, not just AST names. Final census and CLI verification of this reduced scope are pending.

No new table or timer is introduced. The removed assignment-transition metadata and the new required order fields still need an explicit retained-data disposition before installation. No compatibility reader or speculative carry has been added; original sessions, RTS data and historical UNKNOWN operations remain untouched. This PR is not installation-ready until that data issue is resolved.
