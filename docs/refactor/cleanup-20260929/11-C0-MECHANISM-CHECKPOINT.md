# C0 mechanism checkpoint

## Ownership and deletions

TIME-9: the five declared mechanisms inherit one module seal. FieldCodec's
four-line bespoke subclass guard is deleted. Same-module ChildProcess
implementations remain valid; external adapters, including descendants and
multiple inheritance, fail during class creation. Declaration families remain
extension points. The seal derives its module from the mechanism's declaration;
there is no production mechanism roster.

WireValue declares a value-owned custom wire form. The existing FieldCodec
consults it at nested encode, decode, schema and projection boundaries. Plain
record declarations still use their fields and metadata. No record is converted
merely because it already has a convenience to_wire method: methods calling
FieldCodec on self would recurse, and sufficient record metadata needs no custom
encoding. External scalar FieldRepresentation capabilities remain available.

## Verification

- Serial focused codec, read-ledger and native RPC checks: 90 passed; the initial
  new seal test had an incorrect family-membership API call, corrected to the
  existing members_with contract. Corrected seal/codec ownership guards: 5 passed.
- Codec, seal and actual OS child ownership checks: 47 passed. Child tests launch
  and retire real processes rather than replacing the process mechanism.
- The built wheel is installed in an isolated target under
  `/home/ts/.cache/agent-scratch/comms-c0-mechanism-seals-20260929`. Its entrypoint
  receipt records adapter rejection and real CLI register/send/inbox/ack/reopen.
  This is mechanism/entrypoint verification, not a Toad usability claim.
- After normal integration of current main through Core416/423, the rebuilt
  wheel passed 31 existing codec and seal checks from its isolated import target.
  WireValue extends the existing FieldRepresentation dispatch; no parallel
  value decoder or FieldCodec subclass is introduced.

Scratch owner is parent Codex. Purpose: one wheel, disposable isolated CLI root,
wheel import target and verification receipt. No selected live root, global
launcher or installed runtime is changed by this checkpoint. Delete disposable
root/import target after acceptance; retain the small receipt.

## Remaining scope

C0's dispatch measures and arm counts already landed in Core409. Its remaining
site migrations belong to Core417/419 and Toad202/208, with external-site review
and the parent's lifecycle work still required. This checkpoint does not close
C0 or the full goal.
