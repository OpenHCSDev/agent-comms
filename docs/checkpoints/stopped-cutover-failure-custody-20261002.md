# Stopped cutover failure custody

Owner: Schrodinger. Existing `OwnerRestartHandoff` and `StoppedOwnerBatch` own the original in-memory launch evidence and wire resource. `OwnerCutover` owns installation and recovery policy. Arendt owns `owner_launch.py` configuration; Mendel owns the destination-filesystem correction. No public mutation belongs to this branch.

The public goal carry failed with EXDEV after all nineteen owners stopped. `FencedOwnerBatch.complete` let the exception unwind the wire context; the one-use publisher then exited. Original launch environments were deliberately never stored, so they were lost. The audited registry and all thirty-nine protected originals remained unchanged. Ordinary configured source startup is the current recovery; it does not recover extinct transient process environments.

Implement through these existing owners: transfer acquired wire lifetime to the stopped batch, carry that same batch in a typed failure, and let the operation validate an explicit original-unchanged recovery before using the retained original launches. Keep one validation and launch loop. The caller must dispose of a failed batch while its process is still alive; no daemon, credential file, new queue, automatic retry, rollback, provider call or input replay.

Source reasoning and existing-owner/caller analysis precede implementation. Migrate production and one-use cutover consumers together. Validate last with one bounded actual local stopped-owner failure and explicit disposition, plus the relevant existing sanity batch. No new provider operation is needed.
