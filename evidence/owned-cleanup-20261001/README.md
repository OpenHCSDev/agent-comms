# Owned cleanup — 2026-10-01

Newly removed 8 clean merged flat worktrees, 10 computed NRA/mypy cache directories, 4 obsolete installed environments, and 12 unused byte-identical ac498-04..07 saved-source copies. Source branches remain.

Observed filesystem free-space increase across the two removal windows: **1,340,989,440 bytes (1.249 GiB)**. Removed allocated directory/file blocks total **1,509,199,872 bytes (1.405 GiB)**; these differ because installed dependencies include shared hardlinks. Free space at final receipt: approximately **8.77 GiB**. Singer's prior reclamation is excluded.

Exact paths, merge/HEAD evidence, resource sizes, preserved copy-proof hashes and classification are in `receipt.json`. Detailed read-only custody and reverse Git scans remain persistent in `.artifacts/cleanup-einstein-20261001`. No force removal, branch deletion, provider input, public restart or store mutation was performed.

The canonical-wire-transcript source and installed-native06 environment remain because another installed environment imports them. Native761 build remains because no standalone counterpart was verified. Native53b8 donors, current publication/source WTs, ac498-10/11/12, original saved journal/proof, unique copy sidecars and canonical input journals are preserved.
