# Input/row-store rebase review

Source: assembled Core `5767e215562755bff5a69810aa107a3863907761`, based on main `00ff77ae9`; owned Toad remains `20f627fd4e654c8f2d6b15e51f3503caf96a8129`. Compared with the previously reviewed Core `27e59c079710b9d5405725edb82d3f6fe1a9a047`. Source only: no suite, startup probe or native simulation run.

## Verdict

No input/row-store rebase defect found in the traced scope. The incoming row-store implementation is retained, and the input path still acquires actual privacy plus its corresponding snapshot. This is not whole-P23, runtime, installer/carry or Standby acceptance.

## Authority and capture

- `execution/input_origin.py`, `execution/input_ingress.py`, `execution/input_publication_source.py`, `input_disposition.py` and `messaging/publisher.py` have no diff between the two assembled revisions. Both HumanInputOrigin authority acquisitions, the intervening user-identity call, root/claim rechecks, original capture fields and controller/project/goal refusal order remain. The three PublicationSnapshot factories and the original acquisition implementation remain.
- `registry/registry_store.py:493–498` still takes `privacy = private_guard_unlocked()` and the source capture under the same `guarded_read()`, then decodes that captured source and returns the actual RegistryCut with that privacy. It does not substitute the presence document, TaskRegistry or a schema declaration for acquisition authority. The capture protocol at these lines is unchanged.
- `registry/registry_store.py:415–425` validates the private guard before even a cache hit. `RegistryRowStore.captured` reads the committed rows in a SQLite snapshot, reuses only an equal row revision, and restarts from empty when the database identity/counter cannot extend the earlier capture (`registry/registry_tables.py`). Incremental removals are applied before writes, so a removed-and-redeclared name ends present.
- Guard reuse at `registry_store.py:697–712` keys on the root/database identities, guard observation and committed row mark. It no longer relies on a database stat that WAL commits can leave unchanged. The cut's currentness check retains its row revision plus guard observation at lines 164–175.

## Privacy and durability changes retained from main

- `registry_tables.py` is identical to incoming main `00ff77ae9`. Its owner/claim-generation row families preserve the original independent generation sections and tombstones.
- The declaration-only schema path is separate from usable privacy: `registry_store.py:718–725` returns schema declarations; actual acquisition still goes through `_check_guard_unlocked().load` at lines 727–738. PrivateRegistryGuard.load still decodes the namespace and calls verify; it does not grant authority from schema metadata alone.
- `RegistryStore._commit_unlocked` prepares the successor mark and passes the actual row commit through the privacy owner. `PrivateRegistryGuard.write` prepares its digest, requires the committed mark to match, then commits its guard. PublicRegistry delegates the same actual commit without claiming private authority. This is the incoming row protocol, not the old document-byte write restored accidentally during rebase.
- Both original Coordination acquisitions remain module-qualified in PublicRegistry.prepare_native and PrivateRegistryGuard.prepare_runtime. Their schema/participant/guard procedure bodies and original constructor paths/lifetimes remain. The unconditional coordinator module binding remains after privacy declarations.

No source repair or additional plan is needed for this reviewed scope. Friction owns the rebased assembly and its existing startup/consumer/carry qualification; the Registration/Goals repair remains with its current context owner.
