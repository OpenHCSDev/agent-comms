# Owner decisions

## 2026-09-28 — D22: round-two durable wire history

Owner: "Rewrite once into the current format, preserving history in place (plan default)"

Implement one tool in `tools/cutover/`, run at the completed step's quiet install,
verify history preservation, then delete the tool. No pre-cutover reader remains
in production source. No runtime state is treated as permission to replay inputs.
