# Local agent-comms stack

This uv project declares the dependency set agent-comms runs with: Toad, Textual
and ui-theme from their `main` branches, on Python 3.14. `pyproject.toml` owns
source selection. The lock is not committed: each install resolves the latest
mains, and only PyPI releases and stock Pi (`pi-native.sha256`) are pinned.

What runs is never this project's environment. `tools/upgrade_bus.py` builds an
immutable runtime under `~/.local/share/agent-comms/runtimes/` from a clean
worktree of the target revision and this project's resolution, rehearses it on a
copy of the bus and moves `~/.local/share/agent-comms/current` to it; every
installed program, launcher and skill link reads through `current`.

For development in a worktree:

```sh
uv lock --project stack --upgrade --python 3.14
uv sync --project stack --locked --python 3.14
uv run --project stack --locked agent-comms --help
uv run --project stack --locked toad
stack/bin/prepare-pi-native
```

`stack/bin/toad-comms [THREAD]` opens the Toad workspace, where you can create
a new agent. Supplying a thread attaches to that existing owner without sending
a prompt. The installed `toad-comms` is this script as the current runtime
packages it. A merge reaches running processes only through the next install.

The managed launcher follows the installed console entry points and active-route
record. On that active N/K bus, ordinary channel posts notify eligible running
subscribers: bounded triage can ignore a post without a full turn, or engage a
normal coding turn. FULL turns provide native read, bash, edit and write with
cooperative resource claims; these claims are not an OS filesystem sandbox.
Saved stopped owners remain stopped until explicitly started, and uncertain old
inputs are never replayed to prove installation.

Current installed bus/compaction ownership and paired Toad acceptance is recorded in
`current-functional/HANDOFF.md` and
`bus-compaction-integration/HANDOFF.md`.
Earlier C0 acceptance is in `c0-live/HANDOFF.md`. Earlier installed acceptance is recorded in
`presence-integration/HANDOFF.md` and the preceding
`relationship-integration/HANDOFF.md`.
That checkpoint includes the lossless saved-relationship migration, real channel
coding, installed sidebar and saved-history checks. Repeated compaction and
queued-input acceptance are recorded in
`s7-integration` and
`input-drain`. These are specific measured paths, not
a claim that every historical issue or performance limit is resolved. PR176 adds lossless existing-root installation of PR94's scalable private
checkpoint and enables it for fresh managed roots. The current live bus has
completed this migration; see checkpoint-live acceptance
and the [installation procedure](private-checkpoint-install.md).

`prepare-pi-native` installs the Pi release named in `pi-native.sha256` from
its published package and its own `npm-shrinkwrap.json`, then builds a pinned
local copy with native input IDs and writer-fenced session storage. Preparation and launch verify a complete-package content commitment,
including dependencies and resolution metadata (see
[`native-package-provenance.md`](native-package-provenance.md)). Each
manifest gets its own copy, so preparing an update leaves running workers on
their previous package until they restart. The copy
retains the stock Bedrock transport; the script does not change the installed
Pi or make a provider call. An installed Pi is neither read nor needed.
Ambient `NODE_OPTIONS`/`NODE_PATH` are removed; the managed-project bootstrap is
copied into and loaded from the verified package. Native v3 files with complete,
valid ancestry are required; legacy or damaged files are refused without repair.
Preparing a package does not replace processes already using an older copy;
update the managed route and restart idle owners to load that package.
The `toad-comms` launcher uses this copy so a direct prompt can produce the
required native user-start receipt. Existing Pi session directories and files
must be private (0700 directory, 0600 file) before a tracked prompt; the native
preflight refuses unsafe sessions rather than changing their permissions.

Native proof uses one transactional, indexed SQLite journal at the existing
`.input-proof` path. Its schema is generated from `NativeContextJournal`; native
commit validates current source IDs and flushes SQLite plus the containing
directory before emitting a live receipt. Cold recovery visits the current
context's indexed proof, with rollback work bounded by the interrupted transaction;
it does not scan the entire proof history. Historical accepted IDs and UNKNOWN
claims remain in native session history and never authorize replay. Initial schema
publication is atomic and cannot overwrite an existing proof journal.

Old JSONL proof requires the explicit quiet-runtime, one-shot durable conversion
in [native proof cutover](native-proof-cutover.md). There is no runtime legacy
reader. The native session JSONL metadata index remains a separate startup cost;
indexed proof recovery does not claim to remove that history read.

A retained bus is upgraded with `tools/upgrade_bus.py`, which relocks the same
way. The stack's source overrides select editable Core and the UI mains even when
Toad's own manifest pins an older dependency. Preserve active sessions and
uncertain inputs; new Toad launches load the updated packages without restarting
running agents.

The coordination wire defaults to `~/.agent-comms`; set `AGENT_COMMS_ROOT` if
your state lives elsewhere. Installing this project does not launch owners or
submit prompts.

Unread reply counts keep a disposable `transcript_reply_index.v3.sqlite3` in the
wire root. The filename derives from the index schema version, allowing an open
UI to finish using its existing cache while a newly installed UI starts. Existing
transcripts are indexed incrementally; later appends and fresh UI processes use
the index. If that file is removed or damaged, the next read rebuilds it from the
native transcripts. Remove superseded cache files after their UI processes exit;
native transcripts and `read_ledger.json` retain the history and read positions.

## Compaction

Pi's own compaction engine is the only one. Its `CompactionSettings` payload
budget owns Core's immutable 300,000-token retained-context threshold as well
as `reserveTokens` and `keepRecentTokens`. A per-thread
`compaction.enabled: false` cannot disable that Core threshold. Models keep
their real advertised context windows; Pi compacts at the smaller of the
model-safe threshold and 300,000 tokens. That same threshold bounds each
summarizer request. An oversized pre-existing span is summarized in order
through bounded updates instead of being sent as one request that the
summarizer cannot accept.

Threshold compaction runs before a model request at a completed user/tool
boundary or after a completed response, never while a tool is running.
A completed response crossing either threshold uses Pi's standard threshold
reason. Overflow recovery is reserved for an interrupted provider request: it
compacts once, then continues the same context. Context size is Pi's estimate,
reported through `get_session_stats`. A manual compaction is
Pi's `compact` RPC. The thread's task brief (`PI_TASK`) is passed to every
summary as custom instructions, ahead of any instructions given to `/compact`.
Tracked inputs are never re-sent by Pi's provider retry; the only resend is
overflow recovery after the provider refused the request as too long.
