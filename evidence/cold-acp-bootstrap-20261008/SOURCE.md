# Core attachment import ownership

The existing tools expose declarations at package import. CLI command and restart
implementations are now imported by their original ToolRequest.apply consumers.
The original ACP compaction/selected-write/private-drain and foreground execution
methods now import their own implementations. Unused tools ForkSpec,
ChannelActivityCliCommand and config_options.backend imports are removed.
No public tool declaration, field codec, command behavior, registry, native
inheritance, process lifetime, admission or recovery contract is replaced.

AST source evidence: Core src plus held Toad and native src, 862 modules,
zero parse omissions; determining sites in before-consumers.txt. Changed modules
were parsed with the same NRA SourceModule/module_syntax_index, compiled, and
operation-owned imports recorded in after-consumers.txt. Dynamic external imports
are not exhaustively resolved by this AST pass.

## Installed real attachment

One completed original ToadApp open/subscription to the existing agent-comms-ux
owner, PID 477574, canonical frontier 147260275. Original private copied settings;
no provider/user input, owner restart or session fork. SnapshotPublication used
the actual seven captured visible categories, not standalone defaults.

Both runs use installed Toad e630280f29bd2d2d46cca32e30d63939c073315c and native
Textual d8ebcd9dd54bf9647e683758973cc416b96caa4b /126. Baseline ACP Core is
3e713c490e09e29b5e6c110811ace2a8f9d1dff0. Candidate ACP/Core is this source,
built as agent_comms-0.1.0-py3-none-any.whl in the retained Core artifact runtime;
all four changed installed modules equal both wheel and production source.
This is not the held Toad80d9 or native0fbb915d qualification.

| Boundary (seconds) | Earlier baseline | Candidate |
| --- | ---: | ---: |
| Initialize call | 1.681 | 1.235 |
| Session-load call | 0.632 | 1.867 |
| Frontend snapshot publication | 0.408 | 0.355 |
| Snapshot page publication | 0.063 | 0.074 |
| App open to visible history | 3.790 | 4.747 |

App errors: none. Provider inputs: zero. Four visible bodies, no loading widgets.
Bootstrap initialize was 447ms shorter in this one comparison; total opening was
957ms slower. These are observations, not an isolated or repeatable gain claim.
Standalone ACP import was 1.501s versus the prior 1.449s: no demonstrated import
wall-time improvement under differing host load. CLI commands, coordinated
execution, manual compaction bridge and restart queue are absent from the
candidate import profile; required schema/type/declaration imports remain.

The candidate received identity at 2.528s and snapshot at 4.027s, a 1.499s gap
before frontend snapshot publication began. The original owner runs
SubscribeRuntimeRequest.apply -> TranscriptReplay.replay -> to_thread(
TranscriptSnapshotUpdate.capture) between these notifications; proxy subscribe
forwards them then awaits ready metadata. This locates a remaining wait interval,
not a proof that capture alone cost 1.499s or parsed the whole 147MB source.
The live owner was preserved, not instrumented/restarted; internal capture vs
scheduling/transport contributions remain unmeasured. Required ACP schema,
source observation and frontend publication remain; this is not a 5s opening fix.

Nine existing catalog/declaration and authority checks passed against the installed
candidate. One unchanged collaboration argument test expected "Value does not
match" but got "Invalid comms_collaboration arguments: Unknown RelationshipEdit
name: 'start'"; unrelated decoder/expectation not altered. Test runner addopts were
cleared for this bounded selection; no full suite or coverage claim.

The first observer attempt resolved the candidate Python symlink out of its
virtualenv and ACP exited before session-load; it supplies no performance result.
Corrected interpreter command retained the virtualenv path. Private copied
settings/state were removed after original App teardown; borrowed artifacts and
public original session/owner remain intact. Diagnostic control and wheel runtime
remain under .artifacts/cold-acp-bootstrap-20261008 for integration.
