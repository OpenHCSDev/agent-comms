# Explicit private route owner launch

Owner: this branch. Scope: active_route.py, private_nk_entrypoint.py, Comms route binding and OwnerLifecycle launch authority. Parent owns compaction and global integration.

Reported live failure: explicit private root owner starts without the matching root/package launch pins, then ACP refuses attachment before input. Resolve_comms_route currently returns LocalRoute for an explicit/environment root; its bind_owners is empty. Default ActiveRoute binds launch pins.

Extend the existing typed route and PrivateNkLaunch authority. Decode explicit environment pins at the service boundary and retain them through owner handoff. Private marker identity may reject a missing or mismatched authority; it must never grant one. Do not add config stores or infer privilege from a marker.

Acceptance: real isolated CLI and environment root selection, new owner fork, ACP attachment and one native localhost response; missing/mismatched pins denied before owner fork. Preserve failed live inputs and do not restart live owners. Installed acceptance pending; this draft is not live.

Owned scratch: /home/ts/.cache/agent-scratch/comms-explicit-private-owner-launch-20260929. Source and retained evidence remain under /home/ts/wt/comms-explicit-private-owner-launch-20260929.
