# Claude bridge compaction

PR: https://github.com/OpenHCSDev/agent-comms/pull/1921 (do not merge).
Owner: pi-bridge-compact. Base: `2be439d1e`.

## Result and remaining gap

Real Claude Code `/compact`, with its own Monitor watching a private Core bus,
now reports `connected -> compacting -> connected`. The same bridge PID and
registration survive. `CompactingThreadStatus` owns the working presentation
`⌛ Compacting context`; Toad has no new code or summary-string checks.

**The reported loss to `no_bridge` was not reproduced on main.** Main stayed
connected through real compactions with both Claude 2.1.303 and the reporting
session's pinned 2.1.296 executable. This PR supplies the missing connected
compaction state; it does not claim to have diagnosed an unobserved watch exit.
The requested failing-main/passing-branch comparison remains incomplete.

## Cause traced in source

- `cli_bridges/shared.py:289-298`: the watch registers its endpoint for its task
  lifetime. `registration.py:86-105` clears that exact endpoint to `NoProcess`
  and `NoBridgeThreadStatus` when the watch context exits.
- `cli_bridges/shared.py:520-534`: an unobserved probe prints its 15-second
  notice and **continues waiting**. It neither drops registration nor exits.
  A real standalone-watch run reproduced that notice while every CLI sample
  remained connected. Changing that timeout would not fix the reported state.
- `thread_execution.py:165-169`: registration previously replaced every active
  external status with plain Connected, so adding a compaction subclass alone
  would lose it on re-registration. It now takes `requested.with_bridge()`;
  connected states preserve themselves, other active statuses become Connected.
- `manual_compaction_bridge.py:71-138` drives native Pi/ACP compaction. It does
  not observe Claude's external session and must not own this lifecycle.

## Ownership change

`agent_comms_hooks/compaction.py:10-50` declares PreCompact and
SessionStart(matcher=compact) hooks through the existing installer discovery.
The first enters Compacting; the latter is Claude's first explicit
post-compaction observation and restores Connected, including a manual command
with no subsequent model response. Neither unclaimed nor lapsed watches change
presence. No timeout, thread-name special case, second status store, or UI guess.

`WatchClaim` remains the session-to-live-watch binding. Its claim now lives
inside the registered endpoint lifetime (`cli_bridges/shared.py:289-298`), so
it cannot represent a process still waiting to register. A compaction observation
captures that endpoint, rechecks the live watch, and calls Registration's
transaction fenced by incarnation, owner generation and exact process identity
(`registration.py:107-117`). A delayed observation cannot revive a stopped or
replaced bridge. The hook path for ordinary prompts/tools remains stdlib-only.

Before editing, NRA's `--codemod-source-index` indexed 21 bridge/status/hook
modules, 362 classes and their methods. The expanded after index covers 27
modules/415 classes. A follow-through using NRA's existing `ModuleSyntaxIndex`
projects classes, imports, assignments, decisions, comparisons and calls:
4,817 baseline sites and 4,883 changed sites, plus 3,253 sites in seven codec,
process, CLI and Toad consumer modules. Zero parse errors; the new compaction
module is naturally absent from the baseline. This is a bounded source census,
not a whole-repository audit or proof of dynamic plugin binding.

The complete source evidence remains under the scratch owner below as
`source-before.json`, `source-after.json`, `syntax-before.json`,
`syntax-after.json`, and `syntax-consumers.json`. Dynamic metaclass discovery
uses the existing DeclaredFamily/FieldCodec mechanism; supporting family
round-trip tests cover the new member. Toad consumers continue taking the
original ThreadPresentation/status objects (BOUND-8); no second interpretation.

## Real proof

Command (main and branch were separate fresh sessions, never retries):

```
timeout 300 /home/ts/code/projects/agent-comms/stack/.venv/bin/python -X faulthandler \
  tools/probe_claude_monitor_compaction.py main-296 /home/ts/code/projects/agent-comms \
  --claude /home/ts/.local/share/claude/versions/2.1.296

timeout 300 /home/ts/code/projects/agent-comms/stack/.venv/bin/python -X faulthandler \
  tools/probe_claude_monitor_compaction.py branch-296 . --compaction-hooks \
  --claude /home/ts/.local/share/claude/versions/2.1.296
```

All CLAUDE_*, CLAUDECODE, PI_* and inherited AGENT_COMMS_* variables are cleared;
only the private Core root/source binding is supplied. Existing configured
Claude authentication/provider/model is used, not a replacement provider.
MCP is disabled for the scratch session. Claude itself starts Monitor with
`timeout_ms=1800000`, runs a real `/compact`, emits its actual hooks, saves a
real compact boundary and reports `compact_result=success`. A logging shim
passes those actual hook inputs to the branch's hook functions. No fabricated
hook inputs, fake registry or synthetic compaction response.

The installed `agent-comms` CLI's actual `main(['--root', ROOT, 'threads'])`
entrypoint is called once per second in a warmed interpreter: cold process
startup on this machine takes several seconds and would falsify that sampling
interval. Each full returned JSON row and timestamp is retained in
`main-296/statuses.jsonl` and `branch-296/statuses.jsonl`. Both exit 0.

Bridge-owned events, from the real saved Claude transcript, not inferred status:

```
main:   Waiting for this session to observe the monitor probe; inbox remains unread.
main:   Watching compact-proof → dd9feb64-e430-4ef6-aa69-8070c86674e7; unconfirmed attempts retained without replay
branch: Waiting for this session to observe the monitor probe; inbox remains unread.
branch: Watching compact-proof → a3fbae1a-2334-490b-bfaa-97a6c1dbd0fc; unconfirmed attempts retained without replay
```

Main transcript:
`~/.claude/projects/-home-ts--cache-agent-scratch-pi-bridge-compact-main-296/dd9feb64-e430-4ef6-aa69-8070c86674e7.jsonl`.
Branch transcript:
`~/.claude/projects/-home-ts--cache-agent-scratch-pi-bridge-compact-branch-296/a3fbae1a-2334-490b-bfaa-97a6c1dbd0fc.jsonl`.
The main transcript's actual probe/watching events are at lines 22 and 28;
the branch transcript's are at lines 35 and 52. The compact boundaries are
retained there; actual hook records are in each run's `hooks.jsonl`.

Actual hook times (UTC Unix seconds):

```
main   PreCompact                  1792432076.232
main   SessionStart source=compact 1792432088.970
branch PreCompact                  1792432139.708
branch SessionStart source=compact 1792432152.670
```

CLI samples, one row per second after initial registration (the initial
`no_bridge` samples are before the watch attaches, not a compaction gap):

```
second  main (PID 1071569)  branch (PID 1077625)
 4      connected         connected
 5      connected         connected
 6      connected         compacting
 7      connected         compacting
 8      connected         compacting
 9      connected         compacting
10      connected         compacting
11      connected         compacting
12      connected         compacting
13      connected         compacting
14      connected         compacting
15      connected         compacting
16      connected         compacting
17      connected         compacting
18      connected         compacting
19      connected         connected
20      connected         connected
21      connected         connected
22      connected         connected
23      connected         connected
24      connected         connected
25      connected         connected
26      connected         (sampling finished)
27      connected         (sampling finished)
```

The first branch compacting sample is 1792432141.407; its first connected
sample afterward is 1792432154.488. Hook subprocess imports/publication add
roughly two seconds on this machine. No claim of instantaneous hook processing.
No dropped/changed bridge PID during either compaction; no actual Toad UI run
or global installation is claimed.

## Checks

All commands use `timeout 300 ... python -X faulthandler`.

- Scoped status/registration/hook-install checks: **28 passed** in 4.09 s:
  `pytest -n0 --no-cov tests/test_thread_status.py tests/test_watch_guard.py
  tests/test_registration_process_identity.py`.
- Broader check: **43 passed, 5 failed**; four old Monitor fixtures omit the
  now-required transcript `position`, and one staged upgrade fixture cannot
  import `cutover.backup`. Rechecked those two complete files against unchanged
  main: the identical **5 failed, 15 passed**. Reported fixture repairs to their
  existing owners pi-cn-p3 and ci-1370; no weakening/excluding assertions.
  pi-cn-p3 subsequently reported the five direct Monitor inputs repaired in
  PR #1917, with 45 reader/Monitor checks passing. The required position
  predates that reader PR. Those fixture changes are not on this branch.
- Initial broader invocation inherited the project's parallel coverage defaults
  and also failed its whole-project 85% coverage threshold on this focused set.
  Subsequent checks explicitly use `-n0 --no-cov` and persistent scratch basetemp.
- `git diff --check` clean. No merge, runtime publication or restart-queue write.

## Assumed / unresolved

- The original reported disappearance needs its actual watch/Monitor exit and
  registry observations. Neither a successful main compaction nor the probe
  notice establishes that cause. I will not invent failing-main proof.
- Successful Claude compaction emits SessionStart(source=compact), as both
  pinned-version runs demonstrate. An interrupted/failed compaction without
  that hook is not covered by this success-path acceptance.
- Hook installation occurs through the normal reviewed package installation;
  the existing live Claude settings/launchers were deliberately not changed.
- The actual view consumes the backend presentation; this PR checks that
  projection, not physical Toad pixels.

## Scratch and process custody

Owner/purpose: pi-bridge-compact,
`/home/ts/.cache/agent-scratch/pi-bridge-compact`, private buses, concise real-run
logs, source census and bounded pytest output. Original Claude transcripts,
auth, live bus and uncertain attempts are untouched. All owned proof-session
and Monitor processes are stopped after their original successful compactions.
No test input was retried. Keep the concise real proof until PR review; older
exploratory generated output can be retired after checking borrowers.

The provided checkout already existed outside `/home/ts/wt`; no additional
worktree was created. Resource check used the repository's
`tools/agent_resource_check.py` because `/home/ts/bin/agent-resource-check` is
absent. Headroom was warning (3–4.6 GiB available RAM, /tmp 3.9 GiB free), so
sessions ran sequentially and reused installed dependencies.
