# Working on agent-comms and the paired Toad stack

Read the current project prompt at `.pi/APPEND_SYSTEM.md` when available and the
standing owner decisions in `docs/DECISIONS.md`. Follow the latest NRA and
refactor-audit skills. Current owner instructions supersede old plan holds.
Keep the short reminders at the top of that prompt in context after compaction.
Use ordinary language: what changed, what still fails, what happens next.

- Work in persistent isolated worktrees under `/home/ts/wt`. Preserve the dirty
  main checkout, other agents' work, native sessions and uncertain input records.
- Reuse a finished isolated checkout on a new branch. Create another worktree only when genuinely concurrent source changes or a different repository require it. Publish unfinished implementation as a branch checkpoint; stashes are temporary, not delivery. Keep a checkout only while source, an installed package, a running job or retained evidence actually needs that path, and remove closed owned checkouts once published branches and borrower checks permit it.
- Fix a demonstrated defect yourself or assign a named implementation owner at
  the next safe checkpoint. Follow through to the actual affected entry path.
- Use AST for every structural refactor: enumerate declarations, writes, decisions, checks, imports, inheritance and all consumers across the relevant production and dependency roots before editing. Read the resulting sites semantically, find the existing behavior-owning class, and migrate the complete related family in one coherent batch, deleting every competing authority path. Record before/after owner and consumer evidence in the PR; report parse omissions and ambiguous dynamic resolution explicitly. Reuse NRA/refactor-audit tooling instead of copying scanners. AST is source evidence, not a behavioral proof; tests and the affected installed live path come last. Preserve legitimate parallel execution while removing parallel semantic authorities. OpenHCS PR #60, commit 5e8812ee83d0dc8714392445bad3e32fc47a1755, tests/unit/test_cellprofiler_static_deletion_gates.py, is the concrete precedent.
- Choose coherent behavior ownership and migrate every caller. Delete replaced
  code in place. No compatibility aliases, alternate codecs or second caches.
- Use OpenHCS #44, #45, #51, #58 and #60 as concrete architectural precedents:
  shared lifecycle behavior, declaration-derived discovery, widget reuse and
  targeted invalidation, owned state with paint derived from time, and deletion
  of the competing compiler/runtime lattice. Read the source relationships,
  not just the PR summaries. See `docs/refactor/cleanup-20260929/OPENHCS-HISTORY.md`.
  Choose the smallest coherent change that removes the competing decisions
  across all consumers. Prefer deleting unnecessary work to adding a wrapper,
  guard, queue, report or framework. Every added class must own existing behavior
  and eliminate an actual decision or duplicated mechanism. Batch changes and
  final verification; deliver useful checkpoints without repeated ceremonies.
- Multiple inheritance composes capabilities through C3 MRO; inheritance and
  composition are not opposites. Use existing metaclasses, subclass initialization,
  context managers, shared behavior, hooks and mixins where they remove duplicated
  decisions or lifecycle work. Decompose large classes around behavior that can
  actually be owned and shared. Make abstractions load bearing by migrating their
  consumers and deleting the decisions they replace. Progress is the correct
  ownership decision implemented throughout the family, not work performed or
  a locally passing workaround. Apply steering immediately without expanding it
  into a new reporting, tooling or verification project.
- FieldCodec has one implementation owner. Extend its declared field capabilities;
  never subclass it outside `field_codec.py`.
- Focused tests check the implementation. Before claiming usability, exercise the
  installed affected native/ACP/UI path. Extend the existing continuous saved-state
  user journey; controlled provider replies are allowed, real UI/transport/state
  are required. A fork check includes immediate opening and its first actual reply.
- Ship substantial useful checkpoints and install the paired pins after checking
  them. Deferred CI and final latency targets do not hold those checkpoints.
- Retain warm rendered bodies across tab returns within the existing resource
  limits. Scroll preparation follows velocity and destination intent, then shrinks
  when idle. Do not introduce a parallel cache to repair ownership or freshness.
- Keep a concrete checklist and report merged, installed and verified states
  accurately. Retire test processes and owned scratch; check disk/RAM before large
  work. Never replay an uncertain input or restart an active owner for convenience.
