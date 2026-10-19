# Phase 7 checked task draft

Owner: pi-cn-phase7. PR: #1932. Source: 7cf2c518d.

## Exact launch restriction

The user-message delivery frame says:

> Do not send a duplicate reply or launch another agent.

This is not a skill instruction, Core rule or tool refusal. No scratch launch was
attempted, so there is no Core refusal to report. The frame accompanying the
owner's scratch-launch authorization contains this same prohibition. The live
bus has not been used for proof.

## Missing declarations

`src/agent_comms/plan_phases.py:89`, ControlPlanePhase, declares only its dependency.
It inherits `task = UnspecifiedPhaseTask()` from line 33. That is the missing
production task declaration for my phase. `RecordPlanePhase` at line 80 also has
no checked task; it is the prerequisite currently preventing phase 7 readiness.
Other unspecified task slots are CutoverPhase:93, StoryPhase:99,
CommanderPagePhase:105 and SkillPhase:111. Previously delivered phases have
explicit delivery declarations instead. A closed story-related subtask does not
supply a full-phase checked task.

The draft below belongs on ControlPlanePhase.task, not in a phase-name lookup.
It must become a CheckedPhaseTask carrying one PendingProblemOrder. The native
problem ID must be allocated against the intended commander's board, not guessed
from the PR number or an unrelated scratch board. The first dispatch proof is a
slice of this task, not permission to mark the whole phase accepted.

## Draft full-phase order

Repository: OpenHCSDev/agent-comms.
Source owner: src/agent_comms/standing_rules.py:25, StandingRule.
Serves: ControlPlanePhase.

Problem: Ready checked work waits for a manual order, while rotation, load,
review and storage still depend partly on private commander script state.

1. Make StandingRule enforcement start an eligible checked phase or unowned
   checked row through the native order and lifecycle owners. Publish the order
   before execution. Give the Pi unit the declared gpt-6.1-sol selection and its
   own persistent ~/wt/<unit>--P<n> checkout. Do not replay uncertain startup.
2. Move the minute wake into Core's existing scheduling owner. Derive its next
   review from canonical orders and activity. End the wake when its commander
   stops. Delete the replaced rotate loop once native parity is demonstrated.
3. Complete the declared StandingRule members and source-provenanced rule context.
   Derive CommanderLoad and UnitTime from orders, unjudged work and turn records.
   Fork through the existing registered-thread operation; delete private beats.
4. Derive growth and UnitStorage signals from their existing owners. Enforce
   allocation policy before allocating. Keep the harness send boundary checking
   native orders. Delete the script/private-file paths each native owner replaces.
5. Deliver only decision crossings once: addressed replies, deadline crossings,
   merges, failed checks and installed closes. Preserve parallel execution,
   original histories and uncertain inputs. Do not turn counters or repeated
   overdue state into repeated commander messages.

Banned: live-bus proof writes; invented acceptance commands; a second order,
launch, task, rule or state codec; phase-name dispatch maps; private heartbeat
files; treating a closed subtask or a fixture pass as full-phase acceptance.

Works when: On a scratch bus, rotation starts a Pi unit for one explicitly
checked ready phase, records its order, and receives its first real reply. A
second rotation creates no duplicate unit or order. A full native commander
session then runs with no script process; replay of a preserved hour on a copy
produces only the specified decisions, once each, and UnitTime classes sum to
each unit's elapsed time. Stop the scratch commander and observe its wake end.
Record the build's Core/Toad revisions with these results.

Report: Name the started unit, retained order, first reply and build; list the
removed competing paths and every remaining native/parity failure plainly.
Do not close this full-phase task on the initial dispatch-only proof.

## Check construction, not a fabricated green command

PendingProblemOrder.check must run the actual affected native acceptance journey
above and fail if any required observation is missing. The repository currently
has no runner for that whole control-plane journey. The existing 12 focused tests
are useful sanity checks but are not that Check. This PR does not silently put
pytest, grep, or an invented command in that field. Implementing and running the
native acceptance entrypoint remains my work, not missing input from the owner.

Likewise, construction must occur after PendingProblemOrder is available: todos
imports plan_phases, so eagerly importing/constructing an order inside a phase
class would recurse through that boundary. Keep declaration-owned behavior and
lazy task construction rather than adding another phase roster. Coordinate that
shared API change with pi-plan-claims, which owns the phase class.
