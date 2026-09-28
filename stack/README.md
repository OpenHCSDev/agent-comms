# Local agent-comms stack

This uv project installs agent-comms alongside the merged Toad and Textual
forks at fixed commits. It requires Python 3.14. The three source locations
are declared in `pyproject.toml`; `uv.lock` records the
resolved dependency set.

From the repository root:

```sh
uv sync --project stack --locked --python 3.14
uv run --project stack --locked agent-comms --help
uv run --project stack --locked toad
stack/bin/prepare-pi-native
```

`stack/bin/toad-comms [THREAD]` opens the pinned Toad UI on an existing
thread. Link that script into your `PATH` if you want the short command name.
It attaches to the current owner and does not send a prompt.

The managed launcher follows the installed console entry points and active-route
record. On that active N/K bus, ordinary channel posts notify eligible running
subscribers: bounded triage can ignore a post without a full turn, or engage a
normal coding turn. FULL turns provide native read, bash, edit and write with
cooperative resource claims; these claims are not an OS filesystem sandbox.
Saved stopped owners remain stopped until explicitly started, and uncertain old
inputs are never replayed to prove installation.

Current installed bus/compaction ownership and paired Toad acceptance is recorded in
[`current-functional/HANDOFF.md`](../evidence/current-functional/HANDOFF.md) and
[`bus-compaction-integration/HANDOFF.md`](../evidence/bus-compaction-integration/HANDOFF.md).
Earlier C0 acceptance is in [`c0-live/HANDOFF.md`](../evidence/c0-live/HANDOFF.md). Earlier installed acceptance is recorded in
[`presence-integration/HANDOFF.md`](../evidence/presence-integration/HANDOFF.md) and the preceding
[`relationship-integration/HANDOFF.md`](../evidence/relationship-integration/HANDOFF.md).
That checkpoint includes the lossless saved-relationship migration, real channel
coding, installed sidebar and saved-history checks. Repeated compaction and
queued-input acceptance are recorded in
[`s7-integration`](../evidence/s7-integration/) and
[`input-drain`](../evidence/input-drain/). These are specific measured paths, not
a claim that every historical issue or performance limit is resolved. PR176 adds lossless existing-root installation of PR94's scalable private
checkpoint and enables it for fresh managed roots. The current live bus has
completed this migration; see [checkpoint-live acceptance](../evidence/checkpoint-live/HANDOFF.md)
and the [installation procedure](private-checkpoint-install.md).

`prepare-pi-native` verifies the installed Pi 0.85.1 bytes, builds a pinned
local copy with native input IDs, bounded compaction and writer-fenced session
storage. Preparation and launch verify a complete-package content commitment,
including dependencies and resolution metadata (see
[`native-package-provenance.md`](native-package-provenance.md)). Each
manifest gets its own copy, so preparing an update leaves running workers on
their previous package until they restart. The copy
retains the stock Bedrock transport; the script does not change the installed
Pi or make a provider call. Set `PI_STOCK_DIR` if Pi is installed elsewhere.
Ambient `NODE_OPTIONS`/`NODE_PATH` are removed; the managed-project bootstrap is
copied into and loaded from the verified package. Native v3 files with complete,
valid ancestry are required; legacy or damaged files are refused without repair.
Adaptive compaction is enabled by default in the prepared native package.
Preparing a package does not replace processes already using an older copy;
update the managed route and restart idle owners to load that package. The
current managed installation has completed this activation, including repeated
compaction retention and queued-input acceptance. Operator failure handling and
exact-ID no-replay rules are documented in
[`compaction-operator-recovery.md`](compaction-operator-recovery.md); that
runbook is not an activation procedure.
The `toad-comms` launcher uses this copy so a direct prompt can produce the
required native user-start receipt. Existing Pi session directories and files
must be private (0700 directory, 0600 file) before a tracked prompt; the native
preflight refuses unsafe sessions rather than changing their permissions.

Native proof recovery streams the retained journal through AgentSession. There
is no lifetime file-size quota or startup copy of historical message bodies.
SessionManager owns historical tracked-input lookup across all branches. Recovery
validates every row and reserves generation high water; it never re-emits live
acceptance proof. Malformed, truncated or concurrently changed evidence refuses
startup without replaying uncertain inputs.

All three packages are installed from immutable Git commits. Toad also pins
agent-comms in its own manifest, so both agent-comms pins must agree. Update
their revisions in `pyproject.toml`, run `uv lock --project stack`, and commit
the manifest and lockfile together.

The coordination wire defaults to `~/.agent-comms`; set `AGENT_COMMS_ROOT` if
your state lives elsewhere. Installing this project does not launch owners or
submit prompts.

Unread reply counts keep a disposable `transcript_reply_index.sqlite3` in the
wire root. Existing transcripts are indexed once; later appends and fresh UI
processes use the index. If that file is removed or damaged, the next read
rebuilds it from the native transcripts.

## Compaction strategy

Pi settings remain the authority for `compaction.enabled`, `reserveTokens`,
`keepRecentTokens`, and the selected model's context window. The native
`CompactionPolicy` declaration in `native-compaction-policy.mjs` owns adapter
strategy, concurrency, input packing, and summary output limits. The prepared
native copy imports that module; its bytes are pinned in `pi-native.sha256`.

To select a strategy for newly launched owners, set the configuration in their
environment, for example:

```sh
export AGENT_COMMS_COMPACTION_POLICY='{"strategy":"parallel","concurrency":2}'
```

Unspecified fields use declaration defaults. Unknown fields, unsupported
strategies, or invalid values fail before a summary request. `serial` runs the
same bounded algorithm with one worker. `parallel` runs independent source
segments concurrently and synthesizes their results in chronological order.
Large intermediate summaries are reduced through additional bounded levels;
no segment is silently truncated. The declaration-owned strategy `plan(segments, policy)` supplies the ordered
segments and worker limit to one executor. This is a scheduling seam, not an
adaptive trigger or general plugin engine. Provider-native summarization is
not implemented.

This adapter has no authoritative local tokenizer. It conservatively bounds
serialized UTF-8 input against the selected model's token budget, including
headroom, and checks each completed prompt before sending. Parallel work
reduces serial latency; it does not claim lossless summaries or constant-time
processing of arbitrarily long active context. Previously compacted history is
represented by its saved summary and retained recent window.

No-replay is unconditional: a failed or uncertain request is never retried.
Failure aborts other in-flight map requests and prevents new maps or synthesis;
no partial compaction is committed. Provider usage is recorded per response.
Progress counts completed source bytes once, then reports a separate synthesis
phase. Stop/abort applies to all work within the compaction.
