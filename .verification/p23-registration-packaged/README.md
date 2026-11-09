# Original Registration callback closure

Apply `registration-handoff.plan.json` through NRA on friction's owned tree. This follows the applied support, namespace, Pi-session and ingress cuts and the package placement checkpoint **356d83cc039ce74469a30a2817c8275fe90bf527**. Do not replay the old ingress correction or the unfinished registration proposal from the previous directory.

## Native result

Seven stages simulate clean, exit 0 (`native-with-guard-cleanup.log` and `.exit`). The dedicated `promote_guarded_import` for `ActivityRegistration` now passes. The plan's SHA256 is **47472cf3f628687347f8c40bf3a90b3b10f6c20b104bca2f34173b7c9b0307b2**. NRA checkout: **5981d592bd5297342ad8f1f129e6bfdd213906f0**.

1. Parameterize the original `ActivityRegistration` over its actual owner, native owner, snapshot, storage, routing, lease and completion values.
2. Remove its now-unread high imports through the native import operation.
3. Move the original `RegistryPathRead` and `ActivityRegistration` declarations to `registry/registration_reads.py`. `ActivityRegistryRead` stays in its existing package module.
4. Specialize original `Registration` and the original `AgentActivity` constructor with the real concrete types. No stored source or record is replaced.
5. Type the four original notification callbacks against that nominal registration capability, with the original concrete payload types. `MessageDeliveryRead[Message]` remains unchanged.
6. Promote the guarded notification import with NRA's dedicated cycle proof.
7. Delete the activity module's unused guarded imports and copies of bindings already imported at runtime. This step moves no import across a guard: the removed bindings are either unread or already runtime-bound in the original module.

## Preserved source and the one abstract signature change

`verify_originals.py` passed: bodies, concrete positional/keyword defaults, decorators and non-method declarations match across **25 classes** in registration, activity, notifications and registry storage. It separately checks the unchanged `ActivityRegistryRead` and moved `RegistryPathRead`. In particular, `Registration.release_turn = changing(RegistryDocument.release_turn)`, lease acquisition/permission order, concrete `NoTurnRouting()` defaults, notification acquisition and cached-property bodies remain unchanged. See `originals.json` and the 282-line `registration.diff`.

The abstract `ActivityRegistration.lease_local_turn` now spells its optional routing default as an ellipsis contract marker instead of constructing a concrete `NoTurnRouting` in the lower module. The real callable defaults in `Registration.lease_local_turn` and `AgentActivity.begin_turn` stay unchanged. The only production call through the abstract activity contract is `AgentActivity.begin_turn`, which supplies `routing` explicitly. This is an abstract-signature change, not a claim that every original syntax token is identical.

## Evidence boundary

This is **source simulation and source comparison**, not application, startup, C3/MRO, full annotation qualification or installation. Additional concrete annotation imports are visible in the diff and must be covered by friction's combined startup/consumer run. Friction owns application, composition, resulting fixes, merge and installation. No engine change or bypass was made; the separate package source-revision collision remains with its Cloud owner.

`before-source.tar.gz` retains the exact source/configuration input. `source-manifest.json` names its commit and per-file hashes. Generated `before/` and `projected/` trees are local scratch; they include the archived resources/configuration. `author_registration.py` uses the existing replacement renderer from `../p23-runtime-callback-plan` via `PYTHONPATH` and never applies product edits.
