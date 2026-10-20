---
name: always-fork-subagents
description: "Owner rule: dispatch subagents as forks (subagent_type \"fork\"), not fresh agents, unless the brief fully teaches correct maintenance and the paper"
type: feedback
---

Always fork (Agent with `subagent_type: "fork"`) instead of starting a fresh general-purpose agent. Owner, 2026-10-18: "always fork. stop it, fork and let the fork take over", then "unless you already schooled it on correct maintenance and the paper".

**Why:** a fresh agent knows only its brief. My briefs carried the owner's rules (multiple inheritance, no back-compat) but not the paper's definition of slop and the results behind it, so a fresh agent judges shapes by dogma. A fork inherits the conversation, including that understanding (, ).

**How to apply:** default to forks for refactor and design work. A fresh agent is acceptable only when its brief teaches correct maintenance itself (slop = a restated required fact a complete mechanism derives; multiple parents derive crossings; mirrors add disagreeing states; passing tests cannot reveal outside supply) and points at `.claude/verify/PAPER-RESULTS.md`. Supersedes the older habit of fresh agents with written briefs.

**Turn limit (2026-10-18):** owner said "don't use that [turn limit] on ur subagents". Forks stop at ~200 turns and can't be given a limit; they have no definition file (docs: code.claude.com/docs/en/sub-agents.md, `maxTurns` frontmatter). For long work use the user-level agent `correct-maintainer` (`~/.claude/agents/correct-maintainer.md`, `maxTurns: 100000`, system prompt teaches the standard). Definitions load at session start only. If a fork hits the cap anyway, resume it via SendMessage right away.
