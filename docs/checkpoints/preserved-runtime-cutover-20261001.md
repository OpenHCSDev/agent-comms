# Same-format retained runtime cutover

Arendt owns this continuation of the executed PR483 operator. Parent owns public
execution. The original PR483 source and protected preimages remain frozen in
their original worktree and receipt; this continuation does not replace them.

PR484 changes acquisition, not runtime or journal formats. Extend the existing
stopped-owner installation with declared Reset and Preserve members. Reset owns
the existing private preimages and journal retirement. Preserve owns a read-only
audit of the original runtime, input, native, proof and goal bytes, without
resetting the journal or making another full native/proof copy.

Both members use the original audience/incarnation/settings fences, no-client
guard, exclusive route publication and retained all-owner launch. There is one
restart implementation and one stopped-owner seam. A fresh reviewed caller and
receipt are required; an existing attempt is never repeated.

Acceptance is a provider-free acquired-file control covering unchanged bytes,
changed bytes/membership refusal, and resource closure. No public effects,
provider calls or repeated installed native gate are authorized by this document.

The separate PR485 latency investigation remains active. This release seam does
not claim to fix general model/tool latency.
