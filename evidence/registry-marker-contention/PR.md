Fix the real registry-reader failure found by S7 PR285 without weakening owner-only policy or bypassing a durability barrier.

Root cause reproduced with unmodified installed main: while a canonical marker replacement raced a registry read, the failed `lstat` reported mode 0600, correct uid, **nlink=0**. The reader was inspecting an inode retired by atomic replacement, independently of the marker's decode.

WireLog remains the canonical marker owner. Its readers and publisher now share one canonical marker leaf lock; read validates the same opened regular inode under that lock, with no-follow open and strict owner uid / 0600 / exactly-one-link policy. Replacement and parent fsync stay inside the writer boundary. RegistryStore deletes its separate, racing marker stat and trusts the canonical validated marker. Existing bus and registry transaction ordering and durability checks remain.

Lock order is bus/registry (when held) -> marker; marker operations never acquire bus/registry locks. No retry loop, error suppression, policy relaxation, new codec or parallel marker store.

Installed verification so far: actual 1,500 canonical publications versus three independent readers, 44,395 reads, zero failures; checkpoint suite included, **26 passed**. Actual 0644, hardlink and symlink corruption still blocks registry reads, marker reads and sends (**3 passed**). Original red receipt and PR285 measurements preserved. Full 50/100/150-thread S7 cases are running against the installed fix. CI deferred; no live-root or launcher change.
