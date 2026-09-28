# S4 builtin channel ownership closure

2026-09-28 · Pascal · source ready for parent integration; no live changes.

## Source and scope

- Persistent tree: `/home/ts/wt/comms-s4-builtin-channel-closure-20260928`.
- Branch: `codex/s4-builtin-channel-closure-20260928`; base main202 `83b4f8ddc31bc5810b3fa94666bfd90771e6edbb`.
- Original requirement: dispatch `plans/S4-thread-routing.md` Part A1/A2 and OPEN7. This is the assigned original literal/caller closure, not implementation of proposed PF1–PF5.
- Existing determining owner is `channel_targets.BuiltinChannel`: value, aliases, is_alias and canonical. No new family, registry, facade, extension mechanism or persisted format.

## Actual changes and deletion

- `turn_runner.py`: import BuiltinChannel, delete GLOBAL_TARGET, parse_target's default uses ALL.value. These are the only three TurnRunner hunks; assignment/lease/lifecycle bodies untouched. Parent relay is the available coordination path to Darwin O1 (no peer queue exposed in this session); integrate with his rename, retaining these channel hunks.
- `claim_admission.py`: claim publication and release self-sender fallback use ALL.value; authority checks, transactions and UNKNOWN behavior untouched.
- `agent_loop.py`: Participant reply routing uses is_alias/canonical instead of spelling broadcast twice.
- `transcript_route_legacy.py`: saved incoming attribution uses existing is_alias/is_channel_target. Retains committed-delivery, source identity, incarnation and exact prompt checks; saved history fallback is data preservation, not an obsolete client adapter.
- `channel_targets.py`: delete GLOBAL_CHANNEL and unused BROADCAST_ALIASES. `messages.py`, `message_bus.py`, `tests/test_irc.py` migrate all their current imports/uses to BuiltinChannel.ALL.value.
- `tests/test_agent_loop.py`: two stale ActivityLog._load consumers found by the affected run now decode their owned emitted activity fixture through Activity.from_wire; production ActivityLog unchanged.
- Removed public names: `turn_runner.GLOBAL_TARGET`, `channel_targets.GLOBAL_CHANNEL`, `channel_targets.BROADCAST_ALIASES`. No aliases or forwarding replacements.
- Search of current core source/tests finds no remaining removed-name consumers. Paired Toad98 source/tests import none of them; parent says Toad99 is live. No paired production Toad migration identified.

## Literal audit and retained formats

`remaining-literals.json` is the post-change AST census of exact #all/#any/#none/broadcast string constants across all core Python source. Only the four declaration spellings in channel_targets.py remain. Text search additionally reviewed embedded help strings, docstrings and comments: CLI help (#all), tool help (#any), examples and commentary are user-facing format descriptions, not routing decisions. Existing tests and saved golden data intentionally retain literal spellings to catch accidental protocol changes; deriving expected wire bytes from the declaration would hide such changes. Unprefixed all/none in CLI option names, login provider identity and ACP status are unrelated facts and stay unchanged. No user data or native authority changed.

## Focused acceptance

- `focused-tests.log`: **99 passed,2 failed** in6.32s. Both failures were the pre-existing test-only ActivityLog._load calls, not routing/admission failures. Do not call that original run green.
- `repaired-activity-tests.log`: **2 passed** in0.23s after current boundary fixture migration; both original failed cases repaired.
- `alias-boundaries.log`: **3 passed** in0.37s: declaration-derived alias lookup/delivery/history/audience, pending-route channel/DM/alias parity, real thread named broadcast plus rename/ack.
- **104 distinct affected cases covered by passing receipts**, without rerunning the already passing99. Includes actual local stub subprocess participant DM/channel/RPC tool paths, ACP target parsing/prompt routing, strict saved-transcript attribution, channel/IRC routing and real private bus+SQLite selected admission/file-write/UNKNOWN verifier. No actual provider, wheel install, deployed process or live UI claim.
- `git diff --check` passes. No new tests or unrelated runtime repairs; only the two failing fixture consumers and removed constant consumer were migrated.

Commands (existing prepared environment, xdist/coverage defaults disabled):

```sh
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' tests/test_agent_loop.py tests/test_transcript_route_legacy.py tests/test_channels.py tests/test_irc.py tests/test_acp.py::TestTargetPrefix tests/test_claim_admission_verifier.py tests/test_channel_coding_tools.py
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' tests/test_agent_loop.py::TestParticipantActivity
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tests timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -q -o addopts='' tests/test_s4_ownership.py::test_alias_declaration_drives_lookup_delivery_history_and_audience tests/test_threads_scaling.py::test_one_pass_matches_dm_channel_broadcast_and_markers tests/test_threads_scaling.py::test_rename_alias_and_real_thread_named_broadcast_match_existing_scope
```

## NRA and proof limits

`nra-before.json`: full package context,79 detectors/0 omitted, complete exact_compact_global. One reported importer Pi-wire mapping/Message lexical mirror is outside this builtin channel task; no claim that a clean scan establishes completion. The concrete S4 consumer audit and post-change AST census establish this assignment's deletion coverage. Changes are authored direct caller patches, not a claimed NRA DSL/proved transformation. No modules or ownership types were introduced; no repeated broad scan/test gate.

```sh
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/channel_targets.py src/agent_comms/turn_runner.py src/agent_comms/claim_admission.py src/agent_comms/agent_loop.py src/agent_comms/transcript_route_legacy.py src/agent_comms/messages.py src/agent_comms/message_bus.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload agent --raw-findings
```

## Remaining

No source blocker for assigned S4 builtin closure. Parent owns serial integration with Darwin O1 and installed activation; CI deferred. Proposed PF1–PF5 remain queued. Owned3.2MB test artifacts removed after all subprocess tests exited; compact success/failure receipts retained. No shared/live worktree edits, duplicate environment, provider calls or helpers.
