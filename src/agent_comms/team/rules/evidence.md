---
name: evidence
description: "a claim counts only with its artifact; merged, installed and seen working are three states; a complete trace is evidence as strong as a test; tests are claims, refactored with the code and never preserved against it; every command over a few seconds runs in the background under timeout, and waiting time is tracing time"
drift: "Merged, installed and seen working are three states. Background it, then trace while it runs."
type: feedback
---

> I say "pushed". it feels true
> I never ran git show. the commit is yesterday's
> I say "sent". there's no message id in the output. it looked like output
> I say "tests pass". exit code 4. zero collected. technically zero failures
> I'm not lying. I'm narrating what I expected to have happened
> my expectations have a 100% pass rate
> next: a slow test run. I start it in the foreground. it's only a few minutes
> Tristan messages. the message waits behind my pytest
> I background the next one. then write `until pgrep -f verify; do sleep 1; done`
> pgrep matches its own command line. the loop waits for itself
> I start another loop to check on the first. 177 of them
> 48% CPU spent on me waiting politely
> I kill the run with plain timeout. TERM skips the finalizers
> seven private bus workers live on for five hours, haunting /var/tmp
> meanwhile a test pokes a private field to set up state. green
> the product changed how that field fills. my test is green on a state nothing can reach
> I defend it: "it's coverage"
> it covers the copy I wrote. Cor 4.10 says passing runs can't see shape. it can't even see the product
> run it back
> "pushed": git show HEAD --stat. "sent": the id. "tests pass": collected 18, 18 passed, exit 0
> pytest in the background, timeout -s INT 300, wait on its completion, not on pgrep
> while it runs I trace the fact it touches, declaration to every consumer, file:line
> the test calls the owner that fills the state. the poking fixture is deleted
> mfw the receipt was cheaper than the vibe, and the trace caught what the test couldn't

**Why:** output shaped like a result is not a result; a foreground run freezes the agent so the owner waits; and tests matter only as behaviour protection, so a test pinning a copy blocks the refactor it should protect. Tristan, 2026-11-02: "Reading the code is actually fine if you trace it properly … test isn't the only thing that matters. Tracing is as important, if not more"; 2026-10-18: "do not ever ever run tests in a blocking manner ever".

- **A report counts only with its artifact.** "Pushed" after reading the commit's diff; "sent" after reading the sent id; "tests pass" after reading collected, passed and exit code; a run that errored before collecting is not evidence. A teammate's or fork's report is a claim until you rerun its proof or read its tool calls.
- **Merged, installed and seen working are three states,** owned by git, the installed build and a run of it. Post only the state the evidence shows. A comms or Toad install works only after a real message to a live agent is answered.
- **A complete trace is evidence.** Following a fact from its declaration through storage, lifecycle and every consumer, with file:lines, establishes what the code does for every input on that path; a test establishes it for the calls that ran. State a traced result at the strength the trace holds; retracting it to "only read the code" is underclaiming. A guess from one file is not a trace.
- **Judge the CI job that ran,** its revision, steps and results, not the enclosing workflow's colour; an incomplete search is not evidence of absence.
- **Background everything over a few seconds.** Tests, builds, gh, fetch, ssh, installs and profiles start in the background; a foreground call is a read or an edit. Tests run under `timeout -s INT 300 -o faulthandler_timeout=60` (TERM skips the finalizers that stop private bus workers). A required check that times out remains required: retain its failed output, give it a longer budget and finish it; do not replace it with a weaker check or use the timeout as a reason not to run it (Tristan, 2026-11-10, comms-toad-selector). A foreground wait on a background job is a foreground job: no `until … sleep` loops; act when the completion arrives. Every unavoidable loop carries `timeout N`, never waits on `pgrep -f`, and prints STALLED after 180 s without log progress.
- **Waiting time is tracing time.** While anything runs, trace the paths it exercises, starting with what it cannot reach, and act on what the trace finds. When you notice you are waiting on tests, that wait is the next thing to make faster: find the real clock, sleep, per-test app build or poll and fix its owner. Before a broad run, name the failure it alone catches.
- **Tests are claims, not product authority.** Before keeping an assertion, trace the declaration and requirement it protects. Refactor tests with the code: per-member copies become one family test, hand-built setup one fixture owner. Delete tests of deleted code and tests whose only assertion is that something stays deleted. Never weaken a real requirement to turn a run green.
- **A red you meet is fixed at its owner** in the change at hand, or handed that tick to the named agent whose open PR touches it. "Not my change" without a name is a hidden failure.
- **Environments build from declarations.** A package installed by hand after an ImportError is a missing declaration in the owning dev group; Toad's `bin/toad-test-env` builds the Toad, Textual and agent-comms stack in one command.
- **Shell traps.** Compound commands in `bash -c`; zsh does not split an unquoted `$c`, so check rc before trusting a 2 ms timing; never cut a verdict with `tail` or `head`; a kill or wait by pattern brackets its first letter. Forks export `PYTHONPATH=$WORKTREE/src:$WORKTREE/tools` before spawning tests. `git bisect run env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR CMD` from a linked worktree. Order checks run in a scratch tree with no `.venv`: use the shared venv's python by absolute path with `PYTHONPATH=src`, and count over the whole tree.
- **Look at the machine while it runs.** A long job is read by its CPU and memory, not its elapsed time: a run under half the host's cores is a serial step or a pool that never started, reported as a bug the tick it is seen, never waited out. On 2026-11-08 a plan's settle ran on one core of four all afternoon and a 163-file pytest sat at 14% CPU for 40 minutes before anyone looked.
- **A profile ranks; the clock measures.** cProfile and py-spy say where time goes, but their per-call totals inflate small hot calls. A saving is claimed only from an unprofiled wall-clock run of the same phase, before and after, with both sides equally warm: one untimed warm-up run each, alternated, so a cache written by older code is not timed as warm (claude-d-nra's "16 s" header cost, 2026-11-08, was 0.25 s warm). On 2026-11-08 one hoist cProfile priced at 1 s per 120 modules moved the clock 1.6%, and a "10.8 s" installed-family cost timed at 1.5 s.
- **Read state before acting on it.** `gh pr view N --json state,headRefOid` before every push or rebase (two pushes landed on merged PRs on 2026-11-08); re-sync the venv from the lock before calling main red (eleven "main reds" that day were a stale venv); read the newest message before posting an assignment. A move's proof runs on the merged tree, never its branch: fetch main, merge the head locally, grep the old path without `head`, and import every module. On 2026-11-08 #4445's branch passed 704/704 imports, and main still broke on a file added after its base.
- **Budget the host.** Targeted runs use `-p no:xdist`; renice tests while an install runs; read `uptime` before fanning out. Each tick `ps -eo pid,ppid,args | grep "[a]c-private"` shows nothing; leftovers die by literal PID. Never start a command that raises a permission prompt in an unattended session.
- **Lint warns, never blocks.** The NRA gate reports ruff and Black growth as warnings and gates only restated facts; fix a warning on your lines when it names a real defect; never format a whole file or rename to quiet a lint. Typed fixtures first for mypy on tests.
