# Joined tracked proof and completion

Source42aa8c74, Core549. Source controls and installed configured native turn passed.
No dependency on551 public checkpoint schema rebuild or U1.

TrackedTurnSession.context_proof now performs the existing exact _verify_context
through Coordination.run_worker. Tool-start and terminal-result consumers await
the same original proof method; each still verifies its current emitted input and
context against original saved bytes and indexed native proof. An earlier tool
proof cannot replace terminal corroboration or its later request generation.
The original event consumer serializes these reads. Cancellation joins the worker
before acquired evidence, socket or child custody can leave its AsyncExitStack.

NativeToolMode.finish is asynchronous. CodingToolOwner owns resource completion:
empty claims do no work; nonempty release runs through Coordination.run_async,
which creates and closes its own SQLite connection in that worker. The original
coordinator clock, exact admission, claims and all publication/registry fences
remain. CodingToolMode invokes the owner. Claims clear only after canonical
release succeeds. Tool authorization retains its original distinct lifetime;
this change does not move concurrent admission callbacks or weaken completion.

Source review corrected two defects in my unfinished checkpoint: passing the
caller SQLite to the worker, and opening a worker/SQLite for empty completion.
Both replaced paths were deleted before qualification; no adapter or new queue.

Four focused source controls passed in0.76s with the existing534 interpreter:

- actual original evidence plus an independently opened coordinator survive
  repeated cancellation until the worker joins and its connection closes;
- the original caller connection remains usable and original source/proof bytes
  stay unchanged;
- empty coding completion succeeds while the original caller owns exclusive
  SQLite custody, without opening another coordinator;
- fresh native request generations remain observed, and original consumer SQL
  errors are not relabeled as source failures.

These are resource/proof controls, not model/native/UI or seconds attribution.
The final configured saved-fork native turn uses Bohr's cleared existing normal69
485/488 holder with unchanged reviewed native960. A normal declared wheel update
replaced only agent-comms; SDK0.12.1 and all other distributions remained installed.
no new checkout/environment/native copy, source overlay or public action.
Original79cb UNKNOWN and418 NotSent remain protected and are never replayed.

After AST uses the existing NRA Package parser over production/tests/tools.
Zero parse omissions; candidate receiver/MRO ambiguity remains explicit.
No schema/native-format change. Parent's next configured channel journey owns
user-level end-to-end latency acceptance; historic8–16s remain unattributed.

Installed receipt and actual timing are in INSTALLED-ACCEPTANCE.md. Source
production is unchanged from42aa; the later commits publish source/evidence only.
