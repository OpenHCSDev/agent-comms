# Verified disposable-output cleanup follow-up

Executing owner: Mendel; parent-authorized independent storage cleanup.
The accepted source/evidence heads `29a25b4b` / `0ebd4a77` remain preserved.
No production source, SessionRevision implementation, runtime selector, owner,
provider input or retained original proof was changed.

## Removed paths

| Owner | Exact path | Allocated bytes removed |
| --- | --- | ---: |
| Mendel449 | `/home/ts/.cache/agent-scratch/comms-r1-preparing-input-custody-20260930/installed` | 8,015,872 |
| Mendel449 | `/home/ts/.cache/agent-scratch/comms-r1-preparing-input-custody-20260930/wheel` | 757,760 |
| Mendel230 | `/home/ts/.cache/agent-scratch/toad-native-widget-actions-20260930/installed` | 16,850,944 |
| Mendel230 | `/home/ts/.cache/agent-scratch/toad-native-widget-actions-20260930/wheels` | 2,215,936 |
| Schrodinger450 explicit clearance | `/home/ts/.cache/agent-scratch/comms428-native-bundle-20260930/mcp-cache-preparation` | 35,987,456 |
| Schrodinger450 explicit clearance | `/home/ts/.cache/agent-scratch/comms428-native-bundle-20260930/npm-cache/_cacache` | 5,476,352 |
| Schrodinger450 explicit clearance | `/home/ts/.cache/agent-scratch/comms428-native-bundle-20260930/wheels` | 757,760 |
| **Total** | | **70,062,080** |

The total is **66.8 MiB**. The available-space increase measured immediately
around these removals also totaled **70,062,080 bytes**. This is an observed
shared-filesystem delta, not exclusive physical-reclamation attribution.

#449 and #230 are merged (`d16238a5` / `4a0bcc67`). #450 is merged
(`be39e659`); Schrodinger explicitly cleared only its three listed finished
cache/build directories and confirmed no active #450 operation. Before unlink,
installed distribution metadata, direct_url/venv metadata, wheel hashes and
cache lock/recipe metadata were retained in the persistent audit.

The existing ProcessIdentity authority witnessed **144** target-user process
incarnations. Privileged read-only inspection checked cwd/executable,
argv/environment/maps and open descriptors: **zero refs, zero read errors**.
The public registry, installed launcher targets and **135** runtime activation,
venv and package-loader/provenance files also had no target references.
No processes were signalled. Contents had no native journal, SQLite database,
input-proof, video or Git source checkout. This is a scoped target-user census;
it does not claim to exclude every other user's process.

## Protected blockers and retained evidence

- No worktree was removed. The inspected clean merged WTs still anchor original
  raw-proof/provenance/native paths; cleanliness alone is insufficient. Source
  and branches are retained. Dirty #430 files `tests/test_envelope_bus_integration.py`
  and `tests/test_transcript_routes_index.py` remain untouched.
- `comms-native-endpoint-budget-recovery-20260930` native761 and
  `comms428-native-wait-custody-20260930` nativececa candidate/rollback paths
  remain protected, together with all installed/runtime/native prefixes.
- `g458` / `g460`, failed candidates, current #457/#461/#462 installations,
  private/public buses, original native sessions/input-proof/UNKNOWN,
  wire/goal histories and six-agent WIP are retained.
- All #449 helper/input diagnostics and original receipts, #230 native07
  physical-frame/reconnect originals, and #450 build/discovery/provenance logs
  and native packages remain. Retained #453 42MB native histories/image/UI
  proofs and #201 custody/incident proofs were not cleanup targets.

Final resource guard: **home10.2 GiB / root8.2 GiB / RAM19.1 GiB / swap8.9 GiB**,
warning/assert exit2. No new build, test, provider call or agent was launched.
Larger reclamation requires explicit retirement of protected candidates/proofs
or another owner's completed output; this receipt does not authorize it.

Raw metadata/census and per-removal receipt:
`/home/ts/.cache/agent-scratch/mendel-owned-resource-cleanup-20260930/followup/`.
Machine summary: `RESOURCE-CLEANUP-FOLLOWUP.json`.
