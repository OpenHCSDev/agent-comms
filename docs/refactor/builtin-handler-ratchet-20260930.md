# Built-in handler ratchet receiving command

Integration owner: Kepler. Paired source authority: NRA stacked draft15, based
on checkpoint/native-proof-performance-20260914. This draft extends the existing
packaged `agent_comms.debt_ratchet.Measure` family. It does not create a checker
or modify ACP failure production code, native input, public owners or runtime pins.

Use one declaration-owned AST collector from the NRA audit source, with a pinned
lightweight package exposing that same source. The existing StringDispatch,
TypeSwitch and arm measures remain admitted through their original per-function
collectors. Built-in MroDispatch handlers are counted per file outside the
canonical codec; moving a case into another function must not hide growth.

Acceptance: build and install this Core wheel and the exact collector package in
an owned isolated environment. Run the installed `agent-comms-ratchet` against
the real PR421 source before/after and verify rejection with positive built-in
dispatch growth. Exercise the same command's existing string/type function
measures. No mocks of AST, Git, protocol, UI or providers. Preserve historical
source provenance and report the exact package/command identity and deletions.

Protected: dirty live `/home/ts/.agent-comms`, other agents' worktrees, raw UI
proofs, private roots, journals and uncertain inputs. No installed default change.
