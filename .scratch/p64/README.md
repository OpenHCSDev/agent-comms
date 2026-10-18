# P64 / P46

Owner: doctrine-segment. Merged PR #1022 as `948bc40bca9bb9bf1c524e080037169f7b5e6366`; feature branch retired. Installation belongs to Codex and is not claimed here. P44 stays BLOCKED.

Canonical results: `/home/ts/.cache/agent-scratch/rts-results/p64.json` and `p46.json`. Six actual CLI witnesses fail on their own gates. P64: frozen writes 0→2; Toad banned prose 0→5. Merged main versus its first parent exits 0. Full guards: 91 passed; two added Git-context cases passed separately.

Retained evidence: per-head CLI JSON, merged-main JSON, NRA ownership output (341 modules, no parse omissions), test logs, and reproduction scripts. The original invalid Core vocabulary witness and older-dependency failures remain recorded; neither is counted as successful proof.

Generated Git fixtures were deleted after lsof found no active borrowers; the temporary audit-package test symlink was removed. Details: `cleanup.json`. No provider input, live source checkout, saved session, wire or UNKNOWN record was removed.

Tests reused `.scratch/nominal-gateway-candidate/bin/python`; only the installed `refactor_audit` package was source-paired through a temporary PYTHONPATH directory. The older package in this checkout's existing venv produced six unrelated guard failures. No installed package or test assertion was changed to address that mismatch.
