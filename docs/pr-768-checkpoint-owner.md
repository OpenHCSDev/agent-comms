# PR 768: checkpoint-owned carry installation

Owner: core-route-counts. The denied turn-context/regression edits remain
preserved. No live rewrite, merge or product-runtime installation is authorized.

## Ownership change

`CertifiedSourceRead.install_at` consumes the existing authenticated candidate
and takes the destination's canonical lock. It requires a current destination
certificate and identical non-seal marker facts, then stages and verifies the
candidate byte chain without moving or changing its source.

`PrefixCertificate._install` now owns the one index/certificate publication
algorithm. Both fresh `install_private_bus_checkpoint` and verified carry call
it. The former free-function builder body is deleted. The synthetic final-path
test's direct lock/replace/unlink/install recipe is deleted in favor of the
product owner. The one-shot executable gains an explicit `--install-at` for a
separate certified offline destination; default operation remains candidate-only.
No old-row reader is added to production.

Carry retains the destination's required old seal and index until the builder
publishes the replacement. An interrupted replacement is sealed-but-inconsistent
and must refuse reads, rather than falling through to unsealed full-scan
admission. Failure reports UNKNOWN; both original/candidate roots are preserved.
The maintenance caller verifies history preservation against an immutable source
before invoking installation and keeps destination writers stopped.

AST: existing refactor-audit Package reader parsed 305 production modules with
zero omissions before the change. Original owner declarations and installer
consumers were enumerated. After change there is one `_install` builder and two
calls in its owning module. Dynamic callers/monkeypatches are not binding proofs.

Checks: checkpoint/carry/final-location batch: 33 passed, one existing stale
claim-fixture test deselected (missing exact_target, already reported). The
expanded focused carry journey: 7 passed in 5.35 s, including marker mismatch
before publication and injected installer failure after byte replacement; the
failed destination retained its required seal and refused reads, while the
candidate and immutable original hashes stayed unchanged.

Saved-history/installed-executable verification is the next acceptance step.
Synthetic tests and the worktree CLI do not establish that acceptance.
Disposable bounded output owned here: `owner-install-3518*` under
`/home/ts/.cache/agent-scratch/core-route-counts`; remove after borrowers exit.
