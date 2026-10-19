# Automatic merge queue

Owner: pi-install-gate. Draft PR #2023. Not accepted or enabled yet.

GitHub owns queue order, proposed merge identity and failed-entry removal. The
existing debt workflow runs `debt-src`, `debt-tools` and `install-rehearsal` on
`merge_group`; there is no second merge scheduler.

Approval is the existing exact-head `agent-comms/review` verdict, not a new
native GitHub review requirement. All current contributors use the same GitHub
identity, so requiring them to approve their own native PRs would strand them.
A status event runs on the default branch, not its referenced PR head. The
restricted host entrypoint validates the real approved head and dispatches the
workflow on its PR branch. Successful checks then request auto-merge against
that exact head. GitHub builds the singleton merge group and reruns all checks.

## Isolation

The registered repository runner is `ts-gaming-agent-comms-merge` (ID 21), with
labels `self-hosted`, `Linux`, `X64`, `ts-gaming`, `agent-comms-merge`.
The runner namespace exposes only the vendor runtime, its work/log directories,
system binaries and a sealed runner home. Host homes, buses, personal tokens,
SSH agents and desktop sockets are not mounted. Its SSH key is restricted to
loopback and one fixed command. No workflow supplies a shell command, checkout,
interpreter or log location to the host.

`tools/merge_queue.py` decodes GitHub answers with the existing FieldCodec and
ExternalRecord. Declared event/check families own eligibility and behavior.
The receiver checks the actual in-progress GitHub run and its workflow path.
An administrator can dispatch qualification checks. A merge group also requires
approval of its original PR parent. The existing install gate still owns source
coverage, private snapshotting and installer invocation. SSH returns an outcome;
all host diagnostics and bus copies stay outside the runner namespace.

The vendor release is 2.338.0, checked against its GitHub release SHA-256 before
extraction. Owned package, runner state and qualification logs are under
`/home/ts/.cache/agent-scratch/pi-install-gate/`; together with the two existing
checkouts they remain below the current 3 GB limit. P2 remains borrowed for the
pinned read-only audit scripts. Do not retire it while a check needs it.

## Acceptance checklist

- [x] First checkpoint committed/pushed within ten minutes (f0dc3df0c).
- [x] Native queue ruleset accepted by GitHub, staged disabled (25439636).
- [x] Runner registered with the repository, initially offline.
- [x] Minimal runner namespace works without host home mounts.
- [ ] Isolated runner executes the three real checks.
- [ ] Review context comes from P714's existing verdict declaration.
- [ ] New host entrypoint has commander-granted source allocation and clean ratchets.
- [ ] Bootstrap changes are reviewed and merged; production entrypoints use main.
- [ ] Queue enabled, with all three checks required and no bypass.
- [ ] First genuinely approved PR merges through the queue without a commander merge.
- [ ] Runtime/storage/install candidates pass actual two-machine private rehearsals.

The last item is blocked by the existing 3 GB worker allocation versus the
26 GiB desktop bus, independently of runner registration. Private copies are
therefore default-off in the host entrypoint. Non-install-source changes invoke
the real gate and may pass its existing outside-surface result; that is not a
successful private install rehearsal. Do not report it as one.
