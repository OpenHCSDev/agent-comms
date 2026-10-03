# Cold saved Context acquisition

Parent assigned the actual freshly restarted runtime owner: ContextRuntimeRequest
reaches TurnRunner.inspect_context with EmptyNative and refuses before any
native request. Earlier547/548 physical saved-history proof explicitly prepared
the child first. It does not cover this cold path.

Mendel owns NativeCustody inspection and TurnRunner's inspection/preparation
boundary. NativeSessionPreparation already owns the saved launch, worker join,
writer fence, GetState/identity/idle attestation and clean child retention without
sending a prompt. Empty custody should use that existing owner. Retained custody
keeps its lock/currentness check; busy custody keeps the active reader's original
pending-response correlation. Reopen/retiring uncertainty remains refused.

No new inspector, converter, store, model default, replay, provider request,
environment or checkout.593 native/known-commit source remains frozen separately.
Source/all callers first, final actual cold saved runtime request and busy
correlation checks last. Tree presentation remains its existing UI owner's scope.
