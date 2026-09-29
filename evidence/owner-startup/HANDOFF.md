# Detached owner startup diagnostics

Baseline core77c2ac67. Deletes the owner launch's implicit DEVNULL output by connecting its existing DetachedProcess output parameter to the existing private diagnostics owner. No new store, schema, readiness flag, retry, or input replay. Every actual launch gets a private 0600 output file with durable thread identity in its filename; worker Python traceback remains available after process exit. Parent descriptor closes after launch; child's inherited descriptor remains valid.

Ownership: IMPL-13 child launch remains OwnerLifecycle/DetachedProcess; IDEN-1 diagnostic membership derives existing stable_thread_lookup; TIME-9 no alternate codec/adaptor. Current NRA and exact archive refactor-audit patterns reread. No behavior families needed for one output capture mechanism.

Evidence: actual production OwnerLifecycle.start with invalid native launch fails before socket, retains PublicationActivationBlocked Python traceback, owner remains session_file=None, diagnostic mode0600. Focused actual subprocess test passed1 in1.53s. This proves capture, NOT original live failure's cause or resolution.

Authentic live failure ACP02_05_43 session/load openhcs-pr159-viewer-bind-owner owner3978150 missing socket. Process dead/sessionNULL; initial stderr was discarded. Current user's ACP child executable verified runtime-workspace-navigation-20260929, not an old-runtime excuse. Original user inputs/history remain untouched. Immediate installed native fork/open acceptance running separately; production defect remains open until a concrete cause is established.
