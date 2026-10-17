# P107: Todo-owned problems and Goal orders

Owner: core-read-locks. Development starts from current main, independently of
frozen PR1145. No conversion, publication, historical Run, operation5/6 or UNKNOWN
input is executed by this work. Codex alone publishes a reviewed runtime.

Todo remains the problem/assignment authority. Its creator is the commander;
its assignment generation identifies the assigned Goal. Deadline and strike
count belong to that same row. Goal retains a problem reference, not a copy of
Todo identity, commander, assignment or deadline. Existing Goal transitions and
Todo revision checks enforce the relationship.

PR1145's GoalProgress/Delivery families, acceptance subprocess executor,
filesystem-location wrapper, fleet observation wrapper and notification publisher
are not imported. They either duplicate existing state/progress owners or exceed
this task's problems/orders/deadlines/strikes scope. Ordinary progress remains
text and existing delivery/publication owners remain unchanged.

Any required retained-format conversion belongs to tools/cutover and must be
reviewed separately. The frozen converted private root and original wire/session
records remain untouched. No native/UI completion is inferred from historical
CLI readback. New development verification will use a separately owned root.
