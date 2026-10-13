# PR 768: isolated final-location check

Owner: core-route-counts. No live rewrite, merge or product-runtime installation.
The denied source/regression files and UNKNOWN attempts remain preserved.

`tests/test_context_manifest_final_location.py` continues the carry journey:

1. Create a sealed full-form synthetic history and copy its complete root.
2. Confirm that the copied certificate refuses the destination's new inode.
3. Prepare a compact candidate through the existing carry tool.
4. Hold the destination's canonical physical lock with no writers; install the
   candidate bytes at their final path. Retire only the copied derived sidecar
   and remove its stale seal. Call the existing
   `private_bus_checkpoint.py:648` installer at this final location.
5. Check the final inode revision, byte digest, seal, root ID, sequence and
   admission facts. Reopen Comms and read message/context history and indexed
   addressed messages there.
6. Run the worktree environment's real `agent-comms history --everything` and
   `agent-comms context Alice --turn 1` entrypoints against that isolated root.
   Check exact original history and manifest values. No journals/providers are
   accessed. Hashes of the source bus/marker/checkpoint and final installed
   bus/marker/checkpoint remain unchanged by reads.

Result: the new final-location check passed in 2.13 seconds. This proves the
isolated manual installation/certification order, not an automated release
publisher. The one-shot carry executable still only prepares candidates; its
release integration and full retained live-history qualification remain open.
The CLI uses the worktree's editable environment, not Tristan's live runtime.

Disposable output: owner core-route-counts; bounded synthetic roots under
`/home/ts/.cache/agent-scratch/core-route-counts/final-location-3414*`.
Remove after child-process/borrower checks; retain this concise result and test.
PR 768 remains coach-gated and blocked from deployment, including the pending
FieldCodec/regression fixes and once-per-digest captured-text work.
