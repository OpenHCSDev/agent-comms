# Sidebar metrics from the original opened source

The live send profile repeatedly entered viewer_snapshot → display_view_metrics
→ BusDisplayIndex → observe_wire. A scope change strictly decoded every silent
ContextManifest again. The index also reopened the path instead of borrowing the
original descriptor. Presentation already released its shared wire lock before
iteration; this repair does not claim or introduce a lock held throughout paint.

## Change

WireLog now owns a typed opened snapshot instead of the resource tuple. Its
certified implementation is selected only while the original StoreLock owns a
CertifiedSourceRead: marker, device, inode/revision and exact byte cut must agree.
The retained descriptor survives that lock; the SQLite reader does not. It gives
read-only public projection, never current admission or append authority. Later
canonical appends cannot widen its cut. Uncertified snapshots keep the original
strict observation decoder. WireScan, private delivery projections and archive
validation remain strict.

BusPresentation and HistoryViews lend that original snapshot to BusDisplayIndex.
DisplayMetricScope consumes Messages, not raw wire rows. The duplicate index
parser/reopen and observe_wire decision are deleted. Pages share the opened-read
accounting; indexed page and archive-validation callers select its strict base
implementation. No new cache or observation exemption was added.

A scope rebuild at unchanged bus size must replace the existing disposable
checkpoint; previously only a size change caused a write. AppendCheckpoint now
encodes once for integrity and persistence instead of encoding both an unsigned
and signed copy. The existing schema, canonical digest and checkpoint refusal
remain unchanged.

## Verification and limits

The existing display/scope/race/retained-observation checks passed (20 tests).
The two new resource checks passed: a certified cut excludes a genuine later
publication and saves the changed scope; an uncertified cut rejects an unknown
observation. Parse and diff checks passed. Full production AST before the change:
324 modules, zero omissions; dynamic ownership was resolved by the actual locked
source and original context-manager lifetime, not filename/sidecar existence.

Two older page-index tests fail before page acquisition. Their fixture certifies
an empty bus, then rewrites the bus and calls it an archive without rebuilding
the original certificate. The strict source refusal is preserved. Raw failures
and temporary fixture experiments remain in /home/ts/.cache/agent-scratch/hdm01;
the unrelated fixture repair was not retained. Initial checks also stopped
because the owned basetemp parent was absent; no application assertion ran there.

The private mounted Channels App used eight declared threads, 32 authored backend
messages and 64 authored silent context rows (eight segments each), then four
real channel-scope changes. Both runs preserved unread count32 and painted the
changed scope. Median update-plus-headless-paint: 173.5ms before, 97.2ms after.
FieldCodec recursive decodes: 40034 before, 6374 after (84% fewer). Strict public
rows: 384 before, 128 after; all remaining rows are actual messages. This is a
private source App, not live provider response or physical terminal acceptance.
Both runs logged the same optional candidate-maintenance unavailability warning.

The baseline used Parent's finished source checkout, whose four affected hot-path
files were byteequal to this branch's determining base a821c53aa. The Toad source
fixture is the stable 507 root; it is not current-main or latest-installed
product equivalence. Original runtime_fixture owns daemon/child cleanup. No
public input, provider request, installed package change or native loan occurred.

Reproducer: private_app.py; original results: before.json and after.json.
Raw App output and owned fixture data: /home/ts/.cache/agent-scratch/hdm01.
Live 84%-of-one-core response latency remains unqualified; Parent owns compatible
client installation and the affected live check through existing PR702.
