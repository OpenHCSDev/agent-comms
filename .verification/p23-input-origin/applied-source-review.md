# Applied P23 source review

Integration base read: `45ca1ad3a5750d4260e9ece469540016b674b803`. Product changes were read only in friction's checkout; no writes there.

## Ingress preservation

`applied-ingress-body-trace.log`, exit 0, records original AST bodies for HumanInputOrigin.capture/_require_owner/require_ingress/applies/matches_owner, original own field declarations for HumanInputOrigin/InputProvenance, original NativeInputConstraintPin checks after lookup, and unchanged relocated UserThreadLookup/FoundUserThread/NoUserThread declarations. It records hashes of the source read.

Read the actual contracts and providers: InputRootAuthority is inherited by RegistryPrivacy; InputRegistry by RegistrySnapshot; InputAuthorityCut by RegistryCut; InputRegistryStore by RegistryStore; InputRegistration by Registration; InputUserIdentity by Messaging; InputIngressContext/InputTextContext by Comms. Existing implementations and concrete stored fields remain. Comms binds the actual InputDispositions class, so public_text constructs that original store at read time with its original root.

Task bootstrap imports low generic InputPublicationRead. BusReader inherits InputPublicationSource, which inherits that API with original Provenance/StoredInput. The implementation reads original privacy, then resolves the original input; NativeInputConstraintPin still checks its origin before reading privacy.namespace, then checks author, recipient and project. Namespace refusal is not brought forward.

## Found and corrected separately

The applied BusReader originally put direct ABC before InputPublicationSource, whose API ancestor already inherits ABC. This is an inconsistent C3 order, independent of import closure. Reported as my recipe defect in 43b7d969e905, with one existing remove_class_base correction in ingress-mro-correction.plan.json. The C3-only interpreter witness is retained, exit 0; it is not a product runtime test.

A subsequent source read shows BusReader at bus.py:165-171 now lists RouteCounts, ConversationRead, MessageReferencesRead, MessageIdentityRead, InputPublicationSource. Direct ABC is gone; inherited ABC membership remains. That resolves the demonstrated header contradiction, not every possible runtime/MRO obligation.

## Core source verdicts recorded

P23 revision 141 -> 144 accepted these exact retained import/conflict-resolution edits:

- `call_e977af63d64d47c9adfda711783fab14|fc_0f8f297180aaea9c016af126313af487d29ec5bb4a2ead1af6`: turn_context InstructionFile.read names InstructionBytes.read. The original packaged instruction path, decoded text and digest from the same raw bytes are retained on this filesystem installation.
- `call_20d36ad64cda4f008a575c9a368e9017|fc_0f8f297180aaea9c016af126313af487d2b3f5697c1173b4b2`: selected/request imports NativePiTerminalFailure from its actual execution.turn_failure declaration. That same class is raised there and caught by SelectedRequest/private send settlement; RenderedInput retains its original declaring module.
- `call_5ef47069b9ad4d6da2dd09d380b4dd00|fc_0f8f297180aaea9c016af126313af487d2bdd47b92872b0c3b`: SessionLifecycle names the original transport/runtime_requests subscription classes and actual lower SessionClient/SessionConfigurationSource/OwnedSessionLifetime declarations. Subscription consumers at session_lifecycle.py:373,553,570,575 resolve to transport/runtime_requests.py:42,46.

These are source-edit verdicts only. Whole-delivery acceptance, complete cold imports, runtime annotation resolution, consumer runs, merge/install and the real installed-owner check remain separate obligations. The failed CLI invocations remain in context-session-batch-verdict*.log; they wrote no verdicts. The three successful verdicts were subsequently recorded directly with comms_verdict.
