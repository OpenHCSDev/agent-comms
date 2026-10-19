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

- `/home/ts/wt/pi-audit-sub--P1/.artifacts/refactor-audit-submodule`: 19 MiB of build/archive/install verification output owned by pi-audit-sub. Removed after verification and zero lsof borrowers. Generated audit bytecode and the temporary upstream PR body were removed too; concise results are retained here.
- `/home/ts/wt/pi-audit-sub-clone-proof`: never created. StorageRule refused `git clone --recurse-submodules` before execution (git clone forbidden), then also refused `git worktree add` because this path is not a registered checkout of pi-audit-sub. No clone workaround is used or clone success claimed.

The resource helper `/home/ts/bin/agent-resource-check` is absent. Direct snapshot: approximately 26 GiB disk available, 6.9 GiB memory available, swap already in use. Checks are serial and reuse installed dependencies and cached build tools; no parallel fleet or large suite is started.

## Verification

- Standalone pin: `3f27eeffc662220a7b786348595198ee31b5cc51` (candidate head of upstream PR #2).
- Upstream: all 10 unittests passed, including the preserved HedgeCheck through the existing Check family. No AST parse omissions among all 15 production script modules.
- Built wheel and sdist with cached Hatch tools and the installed Python 3.14. Each contains all 40 tracked submodule files, byte-equal to the pinned checkout, with zero missing or extra skill files (including no .git or pycache).
- Ran the real `agent-comms install --project "$PWD"` command against isolated HOME and bus roots, once from source and once from the pip-installed wheel (`pip --no-index --no-deps --target ...`). Registered the private commander first, as CommanderConfig.write requires; an initial empty-bus attempt refused before creating links. All three detected harnesses installed successfully.
- Source install: `.claude/skills/refactor-audit`, `.codex/skills/refactor-audit` and `.config/opencode/skills/refactor-audit` each resolve directly to `/home/ts/wt/pi-audit-sub--P1/src/agent_comms/skills/refactor-audit`. Wheel install: the same three links resolve to the skill resource in that isolated installed wheel. SKILL.md and both representative script files were read through every link and matched the pinned source bytes. SKILL.md SHA256: `ba682d1d35631eac8046608564607e0f89193fe503b42f9dd36935768537f1b5`.
- Ran `scripts/debt_census.py --repo "$PWD" --root src/agent_comms_hooks` through the wheel-installed Claude skill link. It parsed and measured all three real hook modules successfully. This verifies the installed skill tool path; it is not an audit of unrelated Core behavior.
- `git diff --check` passed. Normal development/CI installers no longer point to the historical NRA copy.

The requested fresh recursive clone proof is blocked by StorageRule, not passed. A reviewer can run:

```bash
git clone --recurse-submodules --single-branch --branch feat/refactor-audit-submodule https://github.com/OpenHCSDev/agent-comms.git /home/ts/wt/refactor-audit-review
```

No merge, live installation, launcher switch, restart, active-session write or provider call ran. The user-managed synced tree and P202 checkout remain in their owners' custody; no development worktree was made a live skill installation.
