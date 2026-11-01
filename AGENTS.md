# agent-comms: where things are

The rules are not in this file. Behaviour lives in `src/agent_comms/commander/rules/`,
and those rules follow from the two papers in `src/agent_comms/commander/paper/`
(semantic-completeness, then less-is-harder). A session bound to the bus has them
dealt by the rule gate, and native Pi units carry them in their system prompt. Any
other session reads the papers and then every rule file before working, or runs
`agent-comms-prime`. Tristan's dated decisions, each with its evidence, are in
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
- The stack follows the latest mains of Toad, Textual and ui-theme; ship-and-install.md
  has the relock and sync steps.

## Host resources

- Large disposable output goes in a named directory under `/home/ts/.cache/agent-scratch/`.
- Runtime builds and migration copies keep `/home` above 10 GB free. There is no 3 GB
  scratch cap. (When to start cleaning is behaviour: storage-and-cleanup.md.)
- The headroom helper is `tools/agent_resource_check.py` in the main checkout; there is no
  copy at `/home/ts/bin/agent-resource-check`. It runs with the caller's permissions.
  `free -m` and `df -h /home` read the same numbers directly.

## Permissions

- Privileged tooling is limited to read-only `sudo -n py-spy dump` and
  `sudo -n py-spy record`. No sudo for Python, no `sys.remote_exec`, no debugger or code
  injection into a running process. When attribution needs instrumentation, load it at
  a normal-user launch.
- Live-path verification with Tristan's saved sessions and the configured provider is
  standing authorization. A different paid provider or model, or a separate costly
  external operation, needs his explicit yes.
- Physical UI checks run in plain `st` on an isolated display, never on Tristan's
  desktop, pointer or current X session.

## Tools

- A goal the backend marked BLOCKED is retried with `comms_goal` status `retry` once its
  owner, its commander or Tristan authorizes it.
