# agent-comms: where things are

The rules are not in this file. Behaviour lives in `src/agent_comms/team/rules/`,
and those rules follow from the two papers in `src/agent_comms/team/paper/`
(semantic-completeness, then less-is-harder). A session Core launches carries the
whole deal in its system prompt (native Pi; Claude Code started with `claude-comms`)
and answers it in the gate's audit before its first tool call. Another session bound
to the bus is dealt the files to read. Any other session reads the papers and then
every rule file before working, or runs `agent-comms-prime`. Tristan's dated decisions, each with its evidence, are in
`docs/DECISIONS.md`.

A correction to how agents behave goes into the rule file that owns that behaviour,
never here. This file holds only facts about this host and this setup.

## Checkouts and installs

- The live installation belongs to the main checkout, `/home/ts/code/projects/agent-comms`:
  Core is editable from main and the installed launchers point there. A development
  worktree is never the live installation.
- Development checkouts: `agent-comms checkout add PROBLEM`, or
  `agent-comms repository-checkout add PROBLEM OWNER/REPO` for a sibling repository;
  Core prepares their pinned submodules. Remove one with `agent-comms checkout remove PROBLEM`
  (or `repository-checkout remove PROBLEM OWNER/REPO`); it refuses a tree holding
  uncommitted work.
- Every worktree, uncommitted source, saved session and handoff lives under `/home/ts/wt`.
  Never `/tmp`, `/dev/shm` or another volatile filesystem: the 2026-09-27 reboot erased
  four `/dev/shm` worktrees.
- The stack follows the latest mains of Toad, Textual and ui-theme; no-waiting.md
  has the relock and sync steps.

## Host resources

- Large disposable output goes in a named directory under `/home/ts/.cache/agent-scratch/`.
- Runtime builds and migration copies keep `/home` above 10 GB free. There is no 3 GB
  scratch cap. (When to start cleaning is behaviour: team.md.)
- The headroom helper is `agent-comms-resources scan` (installed by `agent-comms install`);
  pass `--assert-healthy` to exit 2 on any non-healthy limit. It runs with the caller's
  permissions. `free -m` and `df -h /home` read the same numbers directly.

## Permissions

- Agents may use `sudo` for whatever the work needs (Tristan, 2026-11-14: "You can use sudo
  for whatever"): strace, perf, py-spy, `sys.remote_exec`, a debugger. Always pass
  `--nonblocking` to `py-spy record`: by default it pauses the process at every sample, and on
  2026-11-04 that froze a session bridge for over 3 minutes while the bus still said connected.
  A normal user cannot kill a root-owned profiler once it is running, so bound every privileged
  command with `timeout`.
- Live-path verification with Tristan's saved sessions and the configured provider is
  standing authorization. A different paid provider or model, or a separate costly
  external operation, needs his explicit yes.
- Physical UI checks run in plain `st` on an isolated display, never on Tristan's
  desktop, pointer or current X session.

## Tools

- A goal the backend marked BLOCKED is retried with `comms_goal` status `retry` once its
  owner, a teammate or Tristan authorizes it.
