---
name: no-strings-for-identity
description: "The owner hates strings, above all as identity (names that refer to modules, classes, steps, kinds); they are web-dev slop. Use typed objects, or derive from the owner"
type: feedback
---

2026-10-19: carry steps link to their predecessor by a hand-written string (`after = "carry_hunts"`). Two PRs wrote the same string, both passed their gates, and every install of main failed. Owner: "why strings and not enum or an actual object with a proper type?" and "we fucking hate strings, especially for identity, it's web dev crayon eating slop. I'm sure this pattern is present in a lot of places. Very unfortunate it wasn't caught."

**Why:** a string standing in for an identity has no type and no owner. It drifts silently, races between PRs, and survives renames. Nominal identity is the paper's core requirement ().

**How to apply:**
- In review, any string that names a module, class, step, kind, event or member is a blocker (). Replace it with a typed reference, or delete it if the fact is derivable.
- If a reference can't be an imported object (for example, deleted history), that is a sign the fact should be derived, not restated.
- External contracts (Claude Code hook event names, protocol fields) are honoured as declared strings in one place only.
- A ratchet measure for string identity is being built (fork, 2026-10-19). Treat that measure's findings as a work list. See .
