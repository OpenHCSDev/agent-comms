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
`after.json` records the same 316 production and 53 tool modules, zero omissions,
eight related sites, zero competing storage predicates and one retained typed
target predicate. `installed-cohort-receipt.json` and `qualification.log` record
actual `require_original` PASS on installed433 with its separate candidate
activation: 942 installed Git assets plus three forced native assets, all159
protected431 originals unchanged. No package restage, App, provider, publisher or
audience/client admission ran.

Normal main641 integration preserves all package and stack bytes exactly against
merged637 and its retained190ace wheel. The determining tool source is
`daf0692dc32bfd978d052a9013c79d423b6db930`; later evidence commits do not change it.
Its automatic Debt ratchet succeeded in run37204478383. Wider legacy workflow
failures remain visible and are outside this approved two-line tool correction;
no suppression or waiver was added.
