# Explicit private route owner launch

25 production lines deleted. Owner: Core PR413, this branch. Production checkpoint: 93f3a66f, based on merged Core411 ae68bc93. Parent owns Core412 compaction and global integration.

## Cause and ownership

The reported live owner died before ACP attachment with "private N/K ACP session requires explicit matching root and package". Resolve_comms_route selected LocalRoute for explicit/environment roots, whose owner binding did nothing. Default ActiveRoute already bound a launch authority. This was BOUND-2: bypassing the existing PrivateNkLaunch owner, with positional tuple reconstruction in OwnerLifecycle (BOUND-1).

LocalRoute now validates explicit environment pins through the existing PrivateNkLaunch boundary. Relative roots become lexical absolute selections before service construction and project-cwd changes. OwnerLifecycle retains PrivateNkLaunch instead of a tuple; that owner revalidates root/package before process reservation and derives the child's exact environment. Ambient private pins are removed from the handoff and replaced only by this retained authority. The marker can deny an unpinned private launch; it never grants authority. No configuration store, marker-derived grant, compatibility path or state mirror was added.

## Actual installed acceptance

The installed baseline runtime-stream-reply-ratchet accepted a missing-pin CLI launch with exit 0 and reserved an owner PID. The regression assertion failed at that approval. This proves the absent pre-fork rejection; it does not claim a second provider input was delivered.

The final noneditable candidate wheel built from 93f3a66f exited 0 on tests/explicit_private_owner_installed_journey.py with native776. Both relative CLI --root and relative AGENT_COMMS_ROOT paths reject missing, partial, wrong-root-ID and unreviewed-package pins before any owner reservation, diagnostic launch file or provider request. Each then starts a new detached owner, attaches through an actual installed stdio ACP subprocess, and completes exactly one native localhost reply. Durable native history contains exactly one user input per mode, two localhost requests total. Test-owned owners are stopped and private wires removed. No failed live input was retried and no user owner, bus or global installation changed.

43 focused route/entrypoint tests passed, including the existing default ActiveRoute and public symlink handoff contracts. The two existing fake-package handoff unit tests explicitly stub package trust; the installed journey above uses the actual reviewed native package without substituting route, ACP, owner or native implementations.

The production ratchet passed with no measure growth. Latest census adds no string dispatch, type switches, long boolean chains, codec subclasses, foreign absence probes, attribute-by-name access or broad exception handlers. Its one new None probe queries the genuinely optional explicit launch at the boundary. Receipts are committed in evidence/explicit-private-owner-launch/.

## Installation and limits

Global activation stays with the parent integration owner. Install into its reviewed paired runtime with:

    uv pip install --python RUNTIME/bin/python --no-deps 'agent-comms @ git+https://github.com/OpenHCSDev/agent-comms.git@93f3a66f3edc1f2b9e90290fd4f95d3b1a7f5171'

This acceptance covers the owner startup workflow through real CLI/ACP/native processing. It does not certify UI presentation or the separate Core412 compaction fix. CI is deferred.

Owned disposable runtime: /home/ts/wt/comms-explicit-private-owner-launch-20260929/.venv (28 MiB). Owned scratch: /home/ts/.cache/agent-scratch/comms-explicit-private-owner-launch-20260929 (3.5 MiB). Keep source/evidence until integration closure; remove disposable runtime only after no process or route references remain.
