# PF4 concurrent caller contract

PF2 replaces `WireLog._private_marker_unlocked()`'s raw dict with WireMetadata.
Pascal's new `ActiveRoute.observe_root` must use the same decoder/owner:

```python
marker = WireLog(root / "bus.jsonl")._private_marker_unlocked()
root_id = marker.root_id
```

Exact narrow replacement wherever the new observation does an inline access:
`._private_marker_unlocked()["wire_root_id"]` →
`._private_marker_unlocked().root_id`.

This does not add a Comms constructor or lock. `_private_marker_unlocked` keeps
its original filesystem permission/ancestry checks and delegates shape decode
only to WireLog.read_metadata_unlocked/FieldCodec. PF2 does not change route
observation scheduling or cached authority, and does not edit Pascal's tree.

For other actual direct consumers: `marker.claims` is protocol availability,
`marker.last_seq` is durable reserved high-water, and `marker.seal` requires a
checkpoint binding. None is native delivery/ACK/execution authority.

Parent can apply this change with the combined PF2/PF4 merge. No raw schema
fallback or old mapping interface should be restored. PF2's edits to existing
active_route rotate/withdraw are only the corresponding typed attribute accesses;
retain Pascal's new observe_root implementation in full.
