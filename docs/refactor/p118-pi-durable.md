# P118: opt-in Pi Durable (unfinished draft checkpoint)

## Checked checkpoint

- Base: `b5000b3b0`; implementation owner: core-pi-durable.
- Exact npm dependencies: pi-durable, pi-ai and chord **1.3.0**. The packaged
  `src/agent_comms/package-lock.json` retains registry URLs and SHA-512 integrity.
  Install with `npm ci --ignore-scripts --omit=dev` in that package directory.
  Codex owns installation; no installed package or user session was modified.
- `pi_durable_host.mjs` is the sole pi-durable importer. The actual Node/SQLite
  host answered open, inspect, resume, context_usage, entries and close.
  Empty context was 0 tokens, no model window; history was empty.
- Core source imports and seals both backends; the default remains
  `PiNativeBackend`. The durable event converter family has 22 declarations.
- Node syntax and Python compilation pass. These are not worker/UI acceptance.

## 1.3.0 API gaps: neither closed

1. **Design section 2, gap 3:** `dist/harness/compaction.d.ts:53` still declares
   `estimateContext`, but `dist/index.d.ts` and the package exports map do not
   export it. Host alone imports the pinned internal implementation by file URL.
   Core does not estimate tokens. This remains an upstream API risk, not a fix.
2. **Design section 2, gap 4:** `Conversation` in `dist/harness/types.d.ts:588`
   still provides submit/abort/compact, not place-queued-now. No `Interrupts`
   capability is claimed, and no abort-and-replay workaround is implemented.
3. The complete 1.3.0 changelog was read: its additions concern SQLite prefixes;
   fixes concern compaction baseline ordering and base64 memory, not these gaps.

## Owners and consumers

Before editing, read AgentBackend/RuntimeLocation and all capability signatures,
PiNativeBackend child lifecycle and conversions, TurnDelivery's construction in
OwnedTurn.stream, OwnedSend admission, SendOutcome, TurnRunner.backend_for and
idle inspection, plus pinned durable Harness/Conversation/events/storage types.
The existing backend family remains the selection owner; no parallel registry
or new default is introduced. RuntimeLocation's envelope migration belongs to
P117/core-runtime-location; DurableDatabase consumes its agreed contract.

Reused the refactor-audit overlay over Core production before edits (no parser
errors). Reused NRA's Python parser for the source census, serially, enumerating
relevant declarations, writes, checks, imports, inheritance and calls. Evidence:
`.scratch/p118/{owner-before.json,source-census.txt,source-census.err}` in the owned
checkout. `owner-before.json` is the overlay's text output, not JSON. Dynamic
registry resolution was inspected semantically. JS API/host were read and syntax
checked, not AST-scanned; no JS structural census coverage is claimed.

## Still unfinished; do not treat this as usable

- Backend consumes P117's new runtime envelope, not the old abstract `.of()`;
  it needs that branch integrated before locations can be instantiated.
- Worker turns intentionally raise before submission: initial Core InputId and
  generic send-disposition integration are not connected. The recovery methods
  still need P117's typed recovery capability/records.
- Event converters are only partially implemented. Idle frames are decoded and
  discarded; active-turn events are not yet wired to Core. Input disposition,
  tool completion and terminal response conversion need delivery integration.
- Startup recovery commands record ABANDON before abort marking and prohibit
  resume while any unfinished item lacks a decision. Their crash/reopen behavior
  has not yet been exercised. Never replay UNKNOWN original inputs.
- No real provider request was made. No saved-thread copy, installed Toad worker
  reply, screenshot or cold/warm measurement exists. Codex has not built this.

The immediate next change is the coherent P117 integration and Core admission
owner migration, followed by complete event conversion, crash checks and the
configured-provider saved-copy journey. A direct host reply cannot substitute
for that worker/UI acceptance.
