---
name: cleanup-needs-owner-consent
description: "cleanup forks delete a unit's scratch only when the owning unit lists it as disposable; \"off the board\" is not proof"
type: feedback
---

Cleanup forks may delete agent-scratch directories only when the owning unit has named them disposable (or the order that made them is closed AND the unit confirms). "Problem off the board" plus no /proc borrower is not enough.

**Why:** 2026-10-18 20:31 disk-2022 deleted e2e's p499-installed-burst (frozen burst receipts of 101 sent DMs + screenshots) on the off-the-board criterion; no copy survived. Sent-input records count as UNKNOWN-input evidence.

**How to apply:** cleanup orders list candidate dirs per unit and ask each unit to delete its own disposable output; forks delete only recordings/copied roots the unit itself released. Related: [[home-storage-growth]], [[test-cleanup-and-cheap-agents]].

A scratch dir's P-number does not say which open order still reads it. The cleanup order must send each owning unit its list and delete only what the unit releases, or what no open order of that unit references in its last 20 tool calls (activity.log).
A copied root's wire/diagnostics, unknown and attempt records count as UNKNOWN records wherever they sit, so a cleanup never deletes a copied root that holds them.
