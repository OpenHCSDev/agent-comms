# PR 768: manifest-row carry checkpoint

Owner: core-route-counts. This is independently implemented work; the denied
`src/agent_comms/turn_context.py` and `tests/test_turn_context_wire.py` edits
remain unchanged and uncommitted. PR 768 remains blocked from merge/deployment.

## Owner and source evidence

- `wire_record.py:178-207`: `WireScan` owns strict row, sequence and key checks.
- `private_bus_checkpoint.py:62-135`: `PrefixWitness` and `CertifiedSourceRead`
  own root identity, certificate revision and sealed read lifetime.
- `private_bus_checkpoint.py:597-598`: the existing `_chain` owns byte chaining;
  the carry reuses it rather than implementing another certificate algorithm.
- `private_bus_checkpoint.py:648-727`: the existing installer builds row pointers,
  captures the candidate certificate and seals the marker. The carry calls it;
  no second installer or production legacy reader is added.
- `tools/cutover/carry_context_manifest_rows.py`: the one-shot old-row declarations
  decode through FieldCodec outside production. Delete them after acceptance.

The refactor-audit Package AST reader parsed all 305 production Python modules
with zero omissions. The relevant owner declarations were enumerated before
implementation. This is source evidence, not a proof of behavior or dynamic
binding. JavaScript and installed/runtime-version differences are not covered
by that AST enumeration.

## What works

The tool holds the source's canonical shared lock, admits only a final sealed
prefix and never repairs it. It streams full-form rows into a fresh, separate
candidate. Non-manifest rows are byte-identical; observations must decode and
round-trip equal, including contributor positions, measurements, captured text
and journal references. The original byte chain must match its certificate.
The existing installer rebuilds pointers, certificate and sealed marker together.
A second read checks the actual candidate bytes, every manifest pointer's offset,
length and value, the candidate byte chain and original marker facts. Root ID,
sequence and admission floor are retained. The source is checked again before
success is recorded.

`tests/test_context_manifest_carry.py` exercises a sealed full-form fixture
containing two addressed messages around a manifest. Four focused tests include
an actual CLI subprocess, indexed message/manifest reads, exact source-file
hashes, existing-output/root-ID/redirect refusal, changed-source refusal without
repair, and incomplete/unknown-observation refusal. First focused run: two
fixture assertions needed correction (candidate is not a registry root; shared
read reports CheckpointNeedsRepair). Final run: 4 passed in approximately 2 s.
The fixture shrank from 21,084 to 15,787 bytes; unlike the retained live-shaped
samples, most of this deliberately small fixture is captured text.

## What does not work yet

This tool prepares a certified candidate; it does not publish into the live root.
A candidate seal is not a final-install seal: moving files changes inode
revisions. The quiet release path must install/certify at the final location
before restarting readers. The current draft decoder still rejects historical
full rows until carry, and its duplicated validation/regression fixes remain
blocked by the retained claim. The branch predates the current installed
runtime, which successfully reads the sampled historical selected_response row;
that branch/runtime mismatch must not be mistaken for live history corruption.
Once-per-digest captured-text retention is also unfinished. No merge, live
rewrite, installed qualification or autonomous release is claimed.

## Disposable output

Owned by core-route-counts: bounded synthetic pytest roots under
`/home/ts/.cache/agent-scratch/core-route-counts/carry-check-3323*`, used for this
carry check. They contain no original user history and are removed after their
processes exit; concise results remain here. The unrelated retained/UNKNOWN
inputs, denied edits, live bus and original sessions are preserved.
