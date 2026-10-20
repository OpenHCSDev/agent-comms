---
name: rules-serve-their-reason
description: "Owner's standing rule for every rule: satisfy its reason, not its letter; override a rule when breaking it leaves its reason intact"
type: feedback
---

Every rule the owner gives (and every safety rule in briefs and memory) exists for a reason. The aim is that the reason holds, not that the wording is obeyed. When following the letter would defeat the reason, or breaking it leaves the reason untouched, act on the reason and say plainly that I overrode the rule and why. This applies to any rule.

**Why:** On 2026-10-19 the live bus sat down from 00:58 to 10:01. I stopped to ask before restoring the installer's own backup, because three rules ("no hand-editing registry.json", "live state only through upgrade_bus.py", "no deleting live state while the owner sleeps") looked like they covered it. Their reason is to never lose or overwrite state irreversibly. The restore kept the broken bus beside it and lost no messages, so the reason was met. The owner said: "they exist so that the reason [is] satisfied, not for the rule... you can override. this goes for any rule."

**How to apply:** Before stopping on a rule, name its reason. If the action keeps that reason intact (it can be undone, a copy is kept, nothing is lost, nothing is exposed), do it, keep the evidence, and report. Stop only when the reason itself is at stake. Outages cost more than an undoable fix. Related: [[full-autonomy-openhcs]], [[structural-fixes-over-pushing-through]].
