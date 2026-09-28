# TR0: required independent per-class size ratchet

T4 explicitly requires class-size comparison against main; the earlier packaged ratchet omitted it. ClassSize is a member of the existing declaration-derived Measure family. OccurrenceMeasure inherits additive implementation for the three original measures; ClassSize owns complete class inventories and identity matching. No second registry or copied script.

Every existing class has an independent lexical line-span delta, including decorators; growth cannot be cancelled by another class shrinking. Qualified nested names distinguish classes. Unique qualified-name moves retain their main baseline; ambiguous names retain path identity. A genuinely new declaration reports base/delta null until it acquires its first main baseline, allowing decomposition into new owners without fabricating a zero measurement. Removed owners measure zero on head. No cap, allowlist, exception flag or CI hold.

Installed wheel console against real Git repositories:19 passed9.02s, including uncancelled growth, shrink, move, moved growth, new-owner subsequent growth, nested and duplicate names, both production roots, plus all existing11 cases. Receipts class-size-tests.log/build/install retained. Paired117 currently exposes three growth findings which this owner is closing; no completion claim until those are accounted for.
