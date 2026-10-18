## Change
Rename `native_custody.py` / `NativeCustody` to `pi_session_state.py` / `PiSessionState`. `PersistentPiSession` already owns the live session; the renamed type owns its legal states and successor chain. No alias or re-export.

Migrate the complete related family: `PersistentPiSession.native_state`, `SelectedSession.pi_session`, per-turn `resources`, imports, measurement name, comments and all test/journey consumers. No process or shutdown behavior changes; `PiSessionChild.shutdown()` and its sole job factory remain unchanged.

## Source evidence
Before: existing refactor-audit source measurement plus AST consumer census across Core, tests, Toad and ACP; no parse omissions. `/home/ts/.cache/agent-scratch/nominal-pi-main/session-name-before.txt`.
After: repeat AST census and source/wheel/installation receipt in `session-name-after.txt` in the same directory. `git grep -n -E 'NativeCustody|native_custody' -- src tests tools` is empty; `rg -n '^class PiSessionState' src/agent_comms` finds exactly its declaration. `PiSessionChild`'s AST is identical to the merged shutdown implementation. No unrelated restart/input/Toad resource-manager renames.

## Verification
Compileall and diff checks pass. Eight changed production modules match source = wheel = private installation. Twelve distinct private-installed checks pass across two batches: actual native/ACP shutdown, shared shutdown job, interruptions and owner stop, forwarding refusal/defer, lazy imports and both timing outcomes. Evidence: `pi-state-name-installed.txt` and `pi-state-name-timing-final.txt` in the directory above.

The first batch exposed two stale timing fixtures: acquisition now receives the shorter-lived input transport stack, not the whole-child stack. The fixture now registers callbacks on their actual existing owners and asserts input cleanup precedes the timer and both child callbacks remain inside it. No production behavior or timing assertions were relaxed; the original failure receipt is retained.

Shared installation remains Codex-owned and is not claimed here.
