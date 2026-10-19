# Refactor-audit submodule delivery

Owner: `pi-audit-sub`. Branch: `feat/refactor-audit-submodule`.

## Source and copies

The Core tree at `2b2c45e0f` contained no refactor-audit skill copy or installer-specific path. `Harness.links()` discovers directories under the packaged `skills` resource; `Harness.install()` and `Harness.published_links()` consume those links. The only competing source reference was the development dependency on the historical copy in NominalRefactorAdvisor.

The submodule now occupies `src/agent_comms/skills/refactor-audit`. Existing discovery derives its harness link from that directory, with no new runtime path or special-case installer. Development installation reads the shared audit measures from the same checkout. Build/release checkouts initialize submodules.

Compared with standalone main `348ae3dcff8b267e35d74489f66fdc3f4b661c6f`:

- `~/.claude/skills/refactor-audit`: absent.
- `~/.claude/skills/synced/1cf47280-e7d8-44a5-8ce1-bf7684e11e4d_2e03a132-0919-4977-b057-1f319ac2928a/refactor-audit`: differs in SKILL.md, seven reference files, and five script files; missing pyproject.toml, handler_declarations.py and tests. Most differences are older versions superseded by main. Unique family-boundary and writing guidance plus HedgeCheck are preserved in [refactor-audit PR #2](https://github.com/OpenHCSDev/refactor-audit/pull/2). Main's corrected measurement semantics are preserved, not overwritten by the old copy.
- `/home/ts/wt/cn-p4--P202/external/refactor-audit`: clean standalone checkout at `c5b2a5e6445e563590838965133f265a35eed1ce`; differs in all seven pattern references. Its fact-kind additions already have [refactor-audit PR #1](https://github.com/OpenHCSDev/refactor-audit/pull/1); no unpublished changes.
- Agent-comms tree: no pre-existing skill copy to delete. The new gitlink is the only skill source. Claude, Codex and OpenCode installation directories had no direct refactor-audit link.
- `/home/ts/dqdock_blind/docs/refactor-audit` and `/home/ts/code/DQDock/docs/refactor-audit`: analysis documents, not skill copies (no SKILL.md or skill scripts).

Search: all named harness skill roots; agent-comms tracked and ignored tree; bounded source-root walk of `/home/ts/code`, `/home/ts/wt`, `~/.claude/skills`, `~/.config`, `~/.pi` to depth 10. Broader home walks were stopped after 50/60 seconds: runtime state, dependency caches, scientific data and deep unrelated artifacts were pruned, not claimed exhaustively inventoried. User sessions and Codex rollouts were not read or changed. Other agents' checkouts and the user-managed synced skill were compared read-only, not deleted or repointed to this development checkout.

## Upstream source relationships

Existing audit `Package.load` parsed 15 script modules with zero omissions before editing. Read Family registration, PlanPackage/Document, CheckResult, Check subclasses and check_package.py: Check owns discovery; NeedsReview owns advisory rendering; the new HedgeCheck reuses both. No caller list, dispatch or duplicate mechanism was introduced. Dynamic family registration is not established by AST; the application check and unittest exercise it. All 10 upstream unittests passed.

## Owned disposable output

- `/home/ts/wt/pi-audit-sub--P1/.artifacts/refactor-audit-submodule`: small build/archive/install verification output owned by pi-audit-sub; remove after verification.
- `/home/ts/wt/pi-audit-sub-clone-proof`: disposable recursive clone owned by pi-audit-sub; remove after evidence is recorded.

The resource helper `/home/ts/bin/agent-resource-check` is absent. Direct snapshot: approximately 26 GiB disk available, 6.9 GiB memory available, swap already in use. Checks are serial and reuse installed dependencies and cached build tools; no parallel fleet or large suite is started.

## Verification

Pending recursive clone, real isolated installation, and distribution checks. No merge, live installation, launcher switch, restart or provider call is authorized or claimed by this change.
