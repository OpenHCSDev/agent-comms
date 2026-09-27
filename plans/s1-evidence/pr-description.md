Backend-to-ACP events were dictionaries whose cases and payloads were independently recovered by each consumer. This migrates producers and consumers together to frozen event classes, declaration-owned MRO handlers, shared activity/metadata algorithms, and one setting-request correlation map. Typed events publish directly through the existing ACP SDK format; external Pi RPC parsing remains unchanged.

Turn settlement now has one owner. Native stream settlement keeps its early fence, and waiters are released after final output. Relay and manual-compaction completion use the same terminal path, including release when client publication fails. Optional diagnostics, usage, MCP receipts, tool diffs, compaction progress, UNKNOWN input handling, and persistent-session behavior are covered by focused tests.

- Validation: four sequential focused shards passed 308, 68, 171, and 80 tests (overlapping groups); 30 opt-in prepared-native-stack cases skipped. Real subprocess fixtures and the real MCP package/SDK path ran without model/provider calls. Ruff and Black pass.
- NRA before/after: exact compact global package context, 79 detectors, zero omissions, zero findings. These async handler moves are authored transformations, not an NRA native-equivalence proof.
- Scope: no A1/A2 registry or codec duplication; no foundation dependency. Parent cursor/history methods and `_JsonLineReader` are unchanged. Preserve PR95 `b74774f`'s reader bound, native launcher gate, and selected-compaction files when integrating.
- Remaining boundaries: S2 external RPC decoding, S7 orchestration decomposition, prepared native-stack execution, and final integration with concurrent branches. No full repository suite, live runtime restart, deployment, merge, or CI wait.

The implementation decisions, commands, receipts, protected-method hashes, and adoption instructions are in `plans/s1-evidence/HANDOFF.md` and `plans/s1-evidence/validation.md`.
