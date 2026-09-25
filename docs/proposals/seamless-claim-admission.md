# Seamless channel notification, N/K wake, and resource ownership

Status: **draft contract, not an implementation or authorization to launch models**. Rebased onto `OpenHCSDev/agent-comms` `main` at `0cf28939ca820f34639e1b62f8a2ea4b62666bab` after the packaged stack and goal fixes. This proposal remains separate from the merged sidebar route-count projection. The first implementation must be a focused follow-up commit on a freshly checked head.

## User-facing behavior

The agent's workflow stays ordinary cross-thread communication:

```text
comms_send(to="#team", body="Please handle the failing build; @builder owns the fix")
```

Every frozen channel member can discover the committed message. A recipient does **not** have to execute a full coding turn merely to learn it exists. The system decides whether a recipient gets a passive/no-wake notification, bounded triage, or a full wake from the committed audience and message policy. The selected worker uses its normal read/edit/bash tools; it should not need an extra `accept_work` command to coordinate with other agents. At the first proposed modification of a file, the native edit boundary acquires or checks that worker's durable file claim. A conflict is a normal denied tool result identifying the current owner, **before** the write. Claim status is visible in the same thread/channel UI. Completion can release the worker's own exact generation; uncertain or failed work does not silently release or reassign it.

`#all` means all registered members; a tag channel means its resolved members. Visibility, read acknowledgment, wake eligibility, response authority, and file ownership are separate facts. A human collective message with no mention may legitimately choose bounded triage; a passive notification must not accidentally turn into a model launch. A `comms_send` type such as `info` is not by itself a durable passive-wake contract.

## What is already present, and what is missing

* `send_initial_cohort` records a committed private-root initial envelope and frozen audience. `accept_initial_cohort` verifies it and seals all N delivery receipts plus K `WakeClaim` rows in one SQLite transaction. A mentioned member can have a FULL claim; an unmentioned observer has a durable no-wake receipt and no claim. The existing one-shot foreground owner can run a selected claim on a marked private `/var/tmp` root. This is **not** yet every ordinary channel send.
* `Comms.send_message(..., claims=..., releases=...)` synchronously verifies and publishes file-claim transitions with the message under the wire/bus locks. It refuses conflicting claims before appending. Existing singly-linked regular files within a physical worktree are supported; directories, new files, symlinks and hardlinks are not. Release is fenced by owner incarnation and claim generation. The ordinary `comms_send` tool has no claim parameters.
* These are two distinct authorities in different durable stores: the bus controls file ownership; the sealed coordinator receipt controls N/K work eligibility. Today neither requires the other. On the existing marked-private-root path, initialize BOTH the initial-envelope protocol and claim read barrier **while the bus is still empty**; installing the claim barrier after first publication fails closed. The baseline test below exercises both but deliberately proves no wake↔file binding. The Pi extension's `tool_call` hook only reports activity; it does not gate edits. Bash or a child subprocess can change files outside a superficial edit-tool check. **Neither green N/K tests nor file-claim tests prove integrated ownership or filesystem enforcement.**

## Native contract (no new mandatory agent command)

1. **Publication and audience.** At send, resolve the canonical audience exactly once and commit the original message plus its policy and frozen N on the authoritative bus. Seal N delivery receipts and selected K wake claims from the committed row. Keep ordinary `comms_send` input and output unchanged; do not quietly redefine existing channel history or UI read acknowledgments. A receiver's `@mention` only selects a member already in N. Make passive/triage/full policy explicit in trusted message metadata; never infer it from the word “info” or an ACK.
2. **Admission.** Before a selected worker can act on the message, verify its stable recipient lookup, currently owned process incarnation, sealed claim ID/revision, exact bus seq and policy. No-wake observers have no claim ID and cannot gain an execution by calling a lower-level endpoint. Full wake and bounded triage remain distinct; triage cannot silently escalate without an authorized transition. No receipt alone proves that the model saw a particular direct/steered input.
3. **On-demand file claim at the write boundary.** When a selected worker's normal tool proposes editing an existing file, canonicalize its physical path, then under the common wire→bus lock verify its wake/execution identity and the resource projection. If unowned, append one generation-fenced resource-claim envelope bound to the selected wake claim and owner incarnation; if already held by this exact owner/generation, reuse it; if held by another, deny before the write. This is a native internal operation, not a separate model-facing step. File claims are not fabricated merely because someone read a channel message.
4. **Cross-store recovery.** A JSONL bus append and SQLite transaction are not one atomic filesystem transaction. Make the **verified complete durable bus row authoritative** for file ownership; bind it to the sealed wake claim by deterministic identifiers and record an idempotent coordinator admission receipt only after the bus commit. After a crash between these steps, reconcile from the one durable row and refuse to launch/continue a write until the receipt is valid. An uncertain append is not automatically resent, and no `get_state`, UI ACK, or model text is treated as proof of a missing claim. A new claim-envelope version must bind the wake claim ID, expected revision, stable recipient lookup, owner incarnation, original seq, normalized resource(s), and generation; old claim rows retain their existing semantics.
5. **Completion and release.** Publish releases fenced by the exact resource generation and owner incarnation, then settle the corresponding work receipt; never release on an unverified success or process disappearance. A competing claimant may take the file only after the release is durable. Rename of the owner's display name is not owner replacement; true replacement/stopped owner must not inherit a claim or revive uncertain work.
6. **Enforcement boundary.** The first code slice can safely gate native Pi `edit`/`write` calls. It must **not** claim hard enforcement while `bash`, extensions, external editors, or arbitrary child processes can bypass the gate. Production-strength enforcement requires a reviewed filesystem/worktree sandbox or equivalent mediation of every write path, including create, rename and delete. Until then label claims as cooperative and return a clear unsupported result for unmediated writes where possible.

## Proposed first implementation slice

Subsequent focused implementation commits on this **draft PR** should add a typed private-root admission operation and no-provider tests, **without** changing the plain channel tool or starting paid models:

* Choose and version the bus admission envelope; keep the resource transition and wake-claim binding in the same durable row, rather than a loose later notification. Define canonical ID/revision/owner fields and reject a mismatched or no-wake claim before publication.
* Verify the sealed N/K receipt and expected recipient under a documented lock order, then acquire an existing-file resource claim. Make a duplicate accepted operation idempotent; a conflict must not append a partial row. A crash/fault at every pre/post append and SQLite receipt point must reopen safely without a second claim or model turn.
* Add an internal `pre_write_admission` adapter for a disposable fake-Pi edit tool. Return a typed denial before writing if ownership cannot be proved. **Do not attach a cosmetic tool-only hook and call arbitrary shell writes protected.**
* Add a read-only projection used by CLI/Pi/ACP/Toad to show the same owner, generation, selected claim and passive observer state; UI reading the projection must never grant work.

Only after this first slice is reviewed should it be wired into ordinary channel sends and every supported write path. **The initial draft commit is a design and runnable baseline only; it implements none of these behaviors.** `tests/test_claim_admission_baseline.py` shows an unselected N/K observer can still make an independent explicit file claim on the same marked private root, without a wake-claim binding. That is a current-gap characterization, not permission to edit another worker's file or a test of the future write gate.

## No-provider acceptance matrix

| Case | Required observation |
|---|---|
| `#team` N=3 with `@builder`, K=1 | Three delivery receipts; only builder has a full wake claim; two passive/no-wake recipients run zero model turns. |
| Collective unmentioned message | N recipients get bounded triage, not N unbounded full coding turns. |
| Selected worker edits a file | One durable resource claim bound to its wake/execution identity before the edit; other worker's conflicting edit is denied before I/O. |
| Unselected observer invokes admission directly | No resource claim, no model turn, no file change. |
| Concurrent selected workers claim the same file | One winner; losing envelope does not partially commit or silently wait/retry. |
| SIGKILL before/after bus append or SQLite receipt | Reopen from authoritative row; no duplicate claim, phantom owner, lost release, or automatic model replay. |
| Rename, stop, owner replacement, stale revision | Rename preserves exact owner incarnation; replacement/stale authority cannot reuse it. |
| Same owner retries uncertain send or write | No second publication or write without a verified receipt; human disposition if uncertain. |
| Bash/child process/write outside mediation | Explicitly documented unenforced boundary until sandbox protection exists; no claim of native enforcement. |
| Read ACK, Toad reconnect, channel expansion | Delivery/read state never mutates work claim or hides an unseen message. |

## Review and coordination

Keep the initial implementation in a dedicated worktree commit, not a copied `/dev/shm` patch. Run the focused matrix and the existing N/K and resource-claim suites; independently review the **combined exact bytes** for the named admission/uncertainty findings. Merged PR #15 changed `declarations.py` route indexing; its sidebar projection is not claim authority. No live provider is needed for this work. A passing proposal document, existing 77 N/K test passes, or green general CI must not be called an implemented admission bridge.
