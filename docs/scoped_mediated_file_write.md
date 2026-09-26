# Scoped selected-claim file write (bounded API)

`claim_admission.write_selected_claimed_file` is an **explicit** API for one
existing singly-linked regular file under a selected FULL or engaged triage
N/K attempt. It is not installed as a general Pi tool hook. A prior
`verify_selected_wake` or published claim alone is not authorization to write:
each invocation rechecks the exact private root, committed selected source,
active owner admission and turn, coordinator generation/attempt, and current
resource claim under wire → bus → registry → SQLite exclusions. A fail-fast
SQLite write transaction remains held through open, truncate, bounded write,
and `fsync`. A stopped/re-admitted owner, settled attempt, different resource
claim or symlink/hardlink alias fails closed before mutation. `O_NOFOLLOW` and
file inode/link checks prevent an already-changed path from passing admission;
malicious filesystem replacement *during* mutation is outside this cooperative
filesystem model.

The payload is exact `bytes`, at most 1 MiB. A successful call means only the
opened file descriptor's bytes were fsynced, not model acceptance or goal
completion. Failure after truncation or any write is **UNKNOWN**; neither the
API nor caller may automatically retry. The implementation performs no
asyncio await while holding exclusions. A slow or hung filesystem can still
block the calling thread and cannot be assigned a guaranteed deadline; use
this only with an isolated caller as appropriate.

**Limits:** shell commands, Pi-native edits, subprocess/child writes, external
programs, human edits, and other unmediated filesystem mutations do not pass
through this function and are not enforced by it. The claim publication and
this API do not confer a global write permit. Do not describe this narrow
boundary as general child/shell enforcement or infer UNKNOWN input consumption
from a write. No paid provider or live activation is required for tests.
