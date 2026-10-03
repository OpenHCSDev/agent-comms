# Owned turn acquisition and retirement

Original #597 is merged and frozen;534 package code is handed to the receiver.
Its saved595/597 forks, private buses, sessions, credentials and uncertain inputs
are protected. This branch reuses the source checkout and starts no native process.

OwnedTurn currently performs registry/goal admission, transcript checkpoint,
input custody and prompt preparation on the event loop before native acquisition.
Its goal/project and failed-input storage cleanup also runs on that loop.
This is source-proven blocking work, not an attribution of the recorded4.480s
lease-to-request interval or the historical13.562/98.141s intervals.

Use original Coordination.run_worker and ExitStack/AsyncExitStack lifetimes.
Join storage work before resource release, including cancellation before callback
delivery. Keep tasks, queues, controllers and event publication on the event
loop. Exact RegistryOwner, lease CAS, input proof, goal permission and publication
owners remain authoritative. No new state/cache/queue, native method or provider.

Claim: OwnedTurn acquisition, prompt/reservation and storage retirement; existing
related consumers as required. No parent AgentActivity, Arendt native observer,
Sch artifact, SessionContext or #598 private fixture edits. Before/after NRA AST
and semantic reading precede production edits. Checks come after the complete
source batch and address cancellation/resource custody; no unchanged provider
wave. Changed actual-path qualification needs a released existing holder.

Related retirement consumer: InputDrain.finish_turn_inputs for original owned and
selected turns. Its original wire-locked notice settlement joins before loop
capabilities retire; cancellation cannot skip queue/grant cleanup after the write.
