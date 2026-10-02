Freeze original protected membership before deriving carry partitions

Actual #517 goal carry committed successfully, then the publisher mutated its own full protected membership through PreserveRuntimeInstallation.unchanged_protected returning the same mutable set. This guaranteed a false post-carry membership failure.

Use immutable membership at the existing producer/RuntimeInstallation contract, derive the byte invariant once before carry, remove the mutable alias and duplicate partition. No new class/store, no carry replay or public mutation. Parent owns explicit already-committed-carry continuation. Actual goal installed receipt will be read through the target typed declarations; a bounded private membership check prevents this demonstrated regression.
