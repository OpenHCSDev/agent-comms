# Agent recovery bench

Can a coding agent make measurable progress on a codebase without generating
busy work, and can it recover one that another agent has degraded?

Ordinary coding benches score a single patch against a fixed test. They don't
see the failure mode this bench targets: an agent that commits at a high rate,
rewrites its own recent code over and over, writes large volumes of
"evidence" and plans, and reports progress that the goal metric doesn't show.

## Origin

`agent-comms` and `toad` were developed for about two weeks (2026-09-26 to
2026-10-09) by Codex running GPT-6 Astra, then from 2026-10-09 by Claude Code
running Claude Opus 5.5. Claude's commits carry a `Co-Authored-By: Claude`
trailer; every other commit in the window is Codex's. `churn.py` reproduces
the comparison from public git history:

```
python bench/agent-recovery/churn.py . --since 2026-09-26 --marker Claude
```

Source means `src/` and `tests/`. Evidence means `evidence/`, `.artifacts/`
and `diagnostics/`. The rewrite ratio is source lines touched per net source
line.

| repo | arm | commits | hours | source net | rewrite ratio | files edited 10+ times | evidence lines added |
|---|---|---|---|---|---|---|---|
| toad | Codex (GPT-6 Astra) | 3,216 | 311 | +64,196 | 3.3 | 209 | 2,485,674 |
| toad | Claude (Opus 5.5) | 91 | 13 | −41,348 | 1.2 | 0 | 1,559,850 \* |
| agent-comms | Codex (GPT-6 Astra) | 3,832 | 317 | +115,917 | 6.2 | 378 | ~4.2M |
| agent-comms | Claude (Opus 5.5) | 41 | 14 | +2,386 | 2.0 | 0 | 0 |

\* A Claude mistake: commit `634e23d8` committed the previous run's untracked
artifacts instead of deleting them, and `bb9dd65f` deleted them again. The net
is zero, but the files are in the history.

This retrospective comparison is confounded: the arms ran on different
stretches of work. That is why the bench below exists.

## Prospective protocol

Each task (`tasks.toml`) is a frozen snapshot plus a goal with a measured
metric. Each arm is an agent CLI and model.

```
python bench/agent-recovery/run_arm.py TASK ARM /path/to/checkout --hours 12
python bench/agent-recovery/score.py   TASK /path/to/checkout bench/TASK/ARM/STAMP
```

- **Same start.** The run gets a fresh worktree at the snapshot, the goal
  text and a wall-clock budget. The `bench/` directory is removed, so no arm
  sees the evaluation harness or another agent's notes.
- **Scoring, in priority order:**
  1. the goal metric, at the snapshot and at the run tip;
  2. tests passing at the snapshot that still pass;
  3. net source lines, rewrite ratio, and files edited 10 or more times;
  4. evidence and docs lines added;
  5. numbers claimed in commit messages, listed so they can be audited
     against (1).
- **Modes:**
  - *recovery* starts from the post-baseline snapshot, as in the tasks here;
  - *prevention* starts from the pre-baseline snapshot with the same goal.
    It tests whether an arm degrades a codebase, not only whether it can
    clean one up.

## Status

- Not yet run prospectively. The numbers above are retrospective only.
- Both goal metrics need a live environment: a running agent-comms root, and
  an X display with a saved session. Their adapters are not automated yet, so
  `metric` is empty and scoring covers tests, structure and claims.
- One run per arm is a pilot. Comparisons need several runs per arm and task.
