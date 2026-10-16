## P18 / P40 / P41 / P62 — unfinished native runtime integration

Comms owns the selected runtime, package and typed native defaults. The selected
child installation runs its fork helper. FULL entry has one tool decision, and
required Core extensions are explicit even with discovery disabled.

P62 working source now stores NativeArguments, not reparsed argv or a synchronized
model/thinking mirror. One startup_arguments method derives explicit options over
creation defaults; creation, CLI and native consumers use that owner. The earlier
mirror commit is replaced, not accepted.

Main through #1070 is integrated normally. ConfigOptions(comms) remains the
single constructor; the process-default configured() factory is deleted rather
than retained beside it. create(spec) still supplies spec.task as first input.
Prepared fields keep explicit Comms so catalogs inspect the selected installation.
The restart queue and publication caller likewise derive their runtime from Comms.

Latest bounded selection checks: 17 passed, with the saved-session case deselected.
The public CLI parser checks cover default, native-settings override and explicit
options for both new and fork (six combinations). Production parsed-argument
readers are zero; frozen mutation calls in the settings/argument owners are zero.
Whole-Core still has 33 existing frozen mutation sites, retained in the evidence.
These source checks are **not registry rows or fork acceptance**.

Actual private installed P41 verification now succeeds on the typed/main
checkpoint: 373 wheel files matched; configured-provider TRIAGE supplied no
tools, then FULL supplied coding and required Core tools despite disabled
discovery. Real read/bash succeeded, one append occurred, and a nonempty answer
was published. See `.artifacts/native-p41-installed-review.json`.

Actual fork CLI exposed a duplicate helper registration under `-m`: canonical
imports and __main__ both defined the helper classes. The repair gives the
existing helper family its canonical module and leaves the executable boundary
without declarations. RuntimeForkHelper owns request creation; no alias or
registration override. See `.artifacts/native-fork-entry-repair.md`. Fresh
installed fork verification follows this change.

Remaining before completion:
- finish catalog/package/selected-startup fixture migrations;
- integrate the separate shared-family checkpoints (#1026 and #1068 with Toad#726);
- rebuild the small private Core wheel, reusing the reviewed frozen native SDK;
- capture actual default and explicit-model/off fork registry rows and first reply;
- repair the separately reported steering terminal error at its existing owner;
  all clear/pending-input retirement work belongs to doctrine-segment/P71.

Integration source evidence: `.artifacts/native-main-20261016-integration.md`.
Before AST: 1865 modules across both clean inputs, paired Toad and metaclass
registry; after: 1086 merged modules. Zero parse omissions. Temporary source
snapshots have been retired; source Git history and concise results remain.

No reviewed installable pair is offered. Private installed Core contains the
typed/main checkpoint; the fork-entry repair still requires rebuilding and
fresh installed fork verification. No default pointer or live owner was changed.
Original sessions and uncertain attempts remain protected. Historical #984 and
896ae3e2a remain retained for final native-family disposition, not reimplementation.
