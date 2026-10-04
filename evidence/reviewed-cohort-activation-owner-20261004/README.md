# Reviewed cohort activation ownership

`CohortActivation.stage` owns the selected target. `ReviewedArtifact` owns the
activation bytes and `InstalledSourceProof.require_activation` binds all declared
source heads, SDK, native artifact and installer origins to that target.

The publisher also required the activation artifact to be stored at
`target/activation.json`. That competing location predicate prevented an independently
reviewed candidate artifact while the previous activation remained immutable.
Remove that predicate from `ReviewedRetainedSummaryCohort.require_original`.

All typed target, interpreter, original artifact hash, complete source, SDK/native,
distinct journey gate, active route, original default link and temporary-publication
checks remain. No package, runtime, schema, codec or public operation changes.

`before.json` records the original audit Package traversal: 316 production modules
and 53 tool modules, zero parse omissions, four existing owner types and eight
named owner/consumer sites. Generic dynamic receivers are not claimed resolved.
Final after evidence and actual candidate read-only qualification follow this
source checkpoint. No App, provider, publisher or audience/client admission runs.
