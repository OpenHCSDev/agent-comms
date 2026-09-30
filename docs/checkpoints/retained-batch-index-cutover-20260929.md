# Retained batch maintenance cutover

Arendt owns the existing OwnerLifecycle.restart_owners stop-all to launch-all boundary, paired with Mendel430's disposable index rebuild owner. Current435 has no operation between those phases. A new private wire pin validates the current index schema before acquiring a batch, so an operator cannot construct a new-runtime private pin against an old index and then rebuild it afterward.

Extend the existing retained batch custody with one explicit maintenance operation carrying the original writer lock/certificate. Preflight and capture every exact idle original owner before the first stop; execute maintenance only after all selected original processes exit and before any replacement launches. Retain each original launch setting in existing RetainedOwnerLaunch. No operator stop/start loop, second registry, guessed process proof, live original replay or automatic retry after partial failure.

Mendel owns index format, rebuild and writer certificate semantics. Parent owns eventual global activation after Sch215's real gate. This draft is source ownership and implementation scope, not cutover authorization or proof that the live installation changed. Resource436 remains independent.
