---
name: refactor-aggressively-tests-too
description: "User authorizes aggressive OpenHCS refactoring; when tests block it, refactor (de-slop, abstract) the tests rather than preserving them"
type: feedback
---

The user said on 2026-10-09: refactor aggressively. Tests are not production code. When a production refactor would mean heavy test rewriting, refactor the tests too, because they are usually badly under-abstracted. Don't port them one by one.

**Why:** The user cares about production code quality. Tests matter only as behaviour protection, and in practice they are repetitive and under-abstracted, so preserving them blocks structural change.

**How to apply:**
- Never let test churn veto a production refactor.
- When tests break en masse, replace them with fewer, parametrized, family-level tests that protect behaviour, not structure.
- Delete tests of deleted code.
- Never weaken a real behavioural assertion.

**2026-10-18, agent-comms:** "no stupid test theater, refactor the slop out of tests as you go, they're fucking sloppy". Applies to every repo and every change: each touched test file is de-slopped in the same change (per-member copies become one registry-iterating family test; hand-built setup moves to one conftest fixture owner; mocks restating production, private-name/log-string tests and golden files for our formats are deleted). "N passed" is never evidence; briefs and orders require `tests -X/+Y` and the behaviour each kept test pins. #1385 touching 40 test files for one argument is the smell to watch for.

This matches the refactor-audit rule "tests protect behaviour, not structure; one test per family". See  and [[full-autonomy-openhcs]].
