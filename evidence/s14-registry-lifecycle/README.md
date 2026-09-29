# S14 registry lifecycle closure — PR361

**133 production lines deleted; 250 added in seven modules.** Source f8166be7;
normal merge fac03ed1 includes current main12bd75ca (357/359 merged).
Boyle owns this slice. Parent retains installation and live UI acceptance.

## Actual ownership / deletion

- Delete the optional `previous`/`previous_status`/`new_owner` RegistrationChange
  bundle and RegistryDocument.apply_registration. Migrate its actual registration
  transaction and document behavior-test callers to RegistrationChange.apply.
- A public ABC supplies the shared write/epoch implementation. InitialRegistration,
  UpdatedRegistration and explicit owner-change composition own their distinct
  publication, maintenance, prior-goal and turn-admission behavior. An update has
  a required previous Thread/status; a first registration cannot borrow one.
  No closed-family switch, kind roster, second store or persisted transition format.
- ThreadPublicationIdentity captures the actual compaction-publication identity
  using existing ThreadIncarnation and ProcessIdentity, plus role/session/worktree.
  Metadata and goal changes remain distinct from publication identity and executor
  generation. The same original statuses still fence publication and ownership.
- Thread owns registration-history normalization and removing imported turn
  admission. Channel counters cannot be forged; creation identity and completed
  turn provenance are retained exactly as before.
- Turn release compares the existing TurnLeaseFence after canonical alias
  resolution. A rename still releases its own turn; a reused ID or replacement
  incarnation cannot release another turn. Revoked admissions may be cleaned up
  but cannot attest successful completion.
- RegistrySnapshot derives restorable aliases from matching retained incarnations
  and unoccupied names. Registry idle fencing uses active/idle owners and exact
  declaration/admission checks before the existing stopped transition.

One nominal owner answers each question; no compatibility facade or unused
predecessor remains. Durable registry schema, generation domains, maintenance
barriers, publication lock ordering and atomic transactions remain unchanged.
No startup/watchdog, selected dispatch, failure presentation or T4 product edits.

## Real verification

| Boundary | Evidence |
|---|---|
| Actual registry/process/turn release/restoration/maintenance | **44 passed / 4.24s**, `focused-first.log` |
| New registration transitions, alias release, stale idle fence + four existing real owner cases | **11 passed**, `transitions.log`; five package-dependent cases did not run successfully, below |
| Corrected package-dependent actual owner restart and publication/transport fence checks | **5 passed**, `installed-native.log` (sixth case's fixture failure retained) |
| **Noneditable installed, actual two-owner native/ACP continuous journey** | **1 passed / 31.24s**, `saved-restart.log` |
| Scoped existing ratchet | No increases: -7 long chains, -37 per-file chain terms, -7 foreign absence probes; `ratchet.json` |
| Ruff F/E9/I and diff whitespace | Passed, `lint.log` and git diff --check |

The continuous journey extends the existing actual channel-reply test. Both
owners first receive a real ACP input and save native history. A channel question
produces B's native keyed reply and A's automatic IGNORE with actual ACP cursor
feedback. Guarded restart gives both owners new process/admission identities,
preserves their historical incarnation, canonical session path and exact session
bytes, and causes **no input replay**. A fresh attachment then loads A and sends
one new explicit prompt; the saved native input occurs once and retained history
remains a prefix. Five controlled localhost provider calls are asserted. Native,
ACP, registry, SQLite journals, owner sockets and child processes are real; the
provider responses alone are controlled. No paid model calls.

Core imports came from this worktree's noneditable `.venv`, byte-equal to current
source (`installed-imports.json`). Canonical native package:
`/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent`.
No live runtime/root/pin was modified. Installed acceptance is not a claim of
parent's final live activation.

## Retained failures and corrections

- `transitions.log`: PI_COMPACTION_TEST_PACKAGE was omitted from the first mixed
  test command. One owner test and four fixture setups failed for the missing
  environment variable, before the intended package-dependent path. Rerun sets
  the actual immutable package; all five pass in installed-native.log.
- `installed-native.log`: channel delivery/ACP cursor checks passed, but the new
  restart fixture assumed selected-delivery journals populated Thread.session_file.
  They are distinct authorities. The fixture now seeds canonical saved sessions
  by sending normal ACP prompts before the channel exchange. No product code,
  assertion or lifecycle fence was weakened to hide this error. Final entire
  journey passes in saved-restart.log.
- An import-provenance command used system Python instead of the test venv and
  raised ModuleNotFoundError. The corrected venv command records actual imports;
  this was not a product test failure.

## Plan / patterns / remaining work

Latest installed skill matches authoritative refactor-audit.skill SKILL/README;
NRA ownership method and IDEN-1/3/8, IMPL-10/12/14, BOUND-2 and TIME-9 applied.
Existing ratchet guards prevent touched-file chain/foreign-absence growth;
RegistryDocument now has no long chains. Small behavior owners remain below500.
No global scan/new agents under resource restriction. No CI gate proposed.

S14 still has other outstanding scopes (for example scheduler/native-event
boundaries). This closes registry registration/lifecycle identity ownership,
not the entire core refactor. No other owner's work was duplicated.

Owned `.scratch` and `.venv` are removed after process-reference inspection;
cleanup.json records disposal. Source, committed RED/PASS receipts, saved live
sessions and other workers' worktrees are preserved.
