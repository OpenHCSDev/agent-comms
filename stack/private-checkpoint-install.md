# Enable the scalable source checkpoint on an existing managed bus

The checkpoint supports bounded history-proof pages as a private bus grows. New managed roots install it during supervised cutover. Existing roots can use the same installer after updating agent-comms.

Use the installed runtime that the managed launcher will use:

```python
from agent_comms.comms import wire
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint

comms = wire()
witness = install_private_bus_checkpoint(comms.bus)
print(witness.through_seq)
```

The installer holds the canonical bus writer lock, validates the complete existing private prefix, builds the existing index format, and publishes its durable seal. It preserves bus bytes, sequence IDs, inode and root identity. It neither changes execution records nor replays inputs. The migration scan streams existing rows; normal checkpoint reads remain bounded pages.

The private root must already have its claim read barrier. Incomplete/unattested rows, unsettled reserved sequences, redirected files or an existing checkpoint are refused. Do not truncate history or fabricate evidence to force installation. Failure before index publication leaves the old bus usable. An uncertain failure after publication retains the sidecar and the existing read barrier refuses an unsealed index; inspect that attempt before retrying.

This checkpoint certifies source coverage, not native model execution, an acknowledgement or permission to retry UNKNOWN inputs. Native Pi proof-journal growth is a separate issue tracked in issue107.
