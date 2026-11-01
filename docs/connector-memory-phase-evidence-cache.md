# Connector memory: phase-evidence parse cache holds the whole repo

Measured 2026-11-01 on `agent-comms-connector serve` (pid 3049259), runtime
`core-ceeedb957` (= `origin/main` `ceeedb9579`).

## Symptom

- Connector RSS goes 346M → ~1,030M in the first 40 s after start, with no MCP
  traffic (4 `tools/list`, 1 `comms_history` in the log).
- The previous instance peaked at 1.9G over 2h22m (`systemctl` stop summary).
- It is the single largest process on a 15G box that is 7–11G into swap.

## Measurement

Read-only object census run inside the live process with `sys.remote_exec`
(Python 3.14), `gc.get_objects()` grouped by type:

| What | Count | Shallow size |
|---|---|---|
| gc-tracked objects | 4,696,372 | |
| `dict` | 1,201,719 | 336 MB |
| `ast.*` nodes | 1,091,485 | (each node also owns a dict) |
| `ast.Module` | 1,209 | |
| `phase_evidence.QualifiedName` | 65,210 | 3.1 MB |
| `repo.GitCommit` | 32,690 | 1.6 MB |

Every `ast.Module` in the process is owned by `MainModule` objects inside
`MainTree.parsed`; nothing else retains a syntax tree.

`MainTree.parsed` contents (one tree, referenced twice):

- repo: `/home/ts/code/projects/agent-comms`
- 1,209 parsed files, 11.0M characters of source kept as `str`
- 617 under `src/agent_comms`, 14 under `src/agent_comms_hooks`, the rest `tests/**`

Estimate: ~600–700 MB of the ~1 GB is this cache (AST nodes + their dicts +
source strings + derived census objects). The remaining ~350 MB is the import
baseline, already resident 5 s after start.

## Cause

`agent_comms/phase_evidence.py:154-170`, `MainTree.modules`:

```python
for (blob, name), content in zip(fresh, contents, strict=True):
    source = content.decode()
    self.parsed[name] = blob, MainModule(name, source, ast.parse(source, filename=name))
```

Each plan-phase evidence check parses every `.py` under its paths on
`origin/main` and keeps the `ast.Module` and the full source for the life of
the process; an entry is replaced only when that file's blob changes. Phase
evidence whose paths cover whole directories (`src/agent_comms`, `tests/`)
pulls effectively the entire repository into memory on the first
`CommanderHeartbeat` pass.

## Ruled out

- Session transcripts: no `sessions/*.jsonl` open in the process.
- Commit graph (`Repo.graph`): full agent-comms history with messages, 6,340
  refs, is ~41 MB peak when built the same way.
- Registry document: small.

## Not yet measured

Why the long-lived instance grows past 1 GB toward 1.9 GB. Likely candidates:
re-parse churn as `main` moves (old trees freed but allocator arenas not
returned), and other per-pass caches. Needs a second census after a few hours.

## Plan

1. Cache per-blob *facts*, not trees. Each evidence kind reads a small set of
   facts from a module (names defined, imports, calls, qualified names). Key
   those by blob SHA and drop the `ast.Module` and source after extraction.
2. If a check truly needs the tree, parse on demand inside the check and do
   not retain it; `git cat-file --batch` already makes reads cheap.
3. Have the evidence declare the narrowest paths it needs; a phase that
   names `tests/` should not force every test file to be parsed.
4. Acceptance: connector RSS after the first heartbeat stays within ~100 MB of
   the import baseline (~350 MB), and a census shows zero retained
   `ast.Module` between passes. Re-check after 2h for the slow growth.

## How to measure again

Do not repeat the census above. It injected code into the live connector
with `sudo ... sys.remote_exec`, which the standing rules forbid (no sudo for
Python or process-code injection; never attach a debugger to a running agent
process). The numbers stand; the method does not get reused.

- Acceptance needs only RSS: `ps -o rss= -p $(systemctl --user show -p MainPID
  --value agent-comms-connector)` right after start, after the first
  heartbeat, and again after 2h.
- If an object census is needed, launch a separate connector instance as the
  normal user with the census or `tracemalloc` loaded at startup, against a
  copied root, and read it there. The live connector is never the subject.
