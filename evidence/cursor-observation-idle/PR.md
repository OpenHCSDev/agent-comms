## Fix
Delete periodic-contention announcement invalidation from the existing ACP producer. A busy read supplies no authority observation, so the last successfully announced typed CursorEnvelope remains the comparison authority. Existing same_observation excludes only revision; scope and original observation remain decisive. No new cache, UI suppression, delay, availability override, or native request.

Trusted _session_runtime_metadata still invalidates changed observations, including Unavailable on a contended load. Failed notification delivery still invalidates. Replaced owner still publishes unavailable. Pattern IDEN-1: publication facts are not local observation revisions; IDEN-3: original typed observations retain their meaning. Latest NRA/refactor-audit skills and identity catalog reread; touched-file absence/chain/excess-500 ratchets unchanged.

## Actual evidence
- Parent paired RED retained: idle64->65, native3->3, pending ValidateSessionUpdateTask gamma Unavailable revision28.
- Actual flock reproduction RED before fix: unchanged proven fact republished3 instead of2.
- Five focused cursor tests PASS: contention dedup, unchanged unavailable dedup, trusted-load recovery over actual TCP RuntimeServer delivery, replaced-owner invalidation, stale scoped callbacks.
- Installed corrected core based40b66442 + exact Toadc649aeba + Textual609b/native9213 full maintained saved_state_user_journey_pilot.py PASS exit0. Original idle assertion unchanged:58->58/native3->3/work[]/pending0; A/B/A raw0; held-status-store native-page assertion retained; physical immediate fork before native answer; reply/status/paint/no replay.
- Source fixtures use real flock; provider reply alone controlled. Isolated installed full application/ACP/native runtime used. Live installation untouched; parent owns paired activation/live gate. CI deferred.

## Commands / ownership
Own source: /home/ts/wt/comms-cursor-observation-idle-sol-20260929; own detached Toad verification: /home/ts/wt/toad-cursor-idle-verification-sol-20260929. No Tesla preparation/UI changes. PR186 remains separate ready channel-history scope.

Focused: PYTHONPATH=src <owned-installed-python> -m pytest -o addopts='' -q tests/test_cursor_load_recovery.py tests/test_acp_private_nk_delivery.py -k 'cursor or replaced_owner'

Installed journey: from owned detached Toadc649 tree, AC_NATIVE_COPIED_PACKAGE=.../native-current-9213ee71479d1b20/node_modules/@earendil-works/pi-coding-agent L0A_EVIDENCE=<this-receipt>/native TMPDIR=$PWD/.artifacts PYTHONPATH=$PWD/tests PATH=<owned-installed-bin>:$PATH timeout180s <owned-installed-python> tests/saved_state_user_journey_pilot.py.

A first modified focused test called the entire session metadata while deliberately holding its goal lock and blocked at goal_snapshot (diagnostic retained); corrected test uses the actual trusted runtime-metadata owner, matching existing TCP recovery test. No production catch or retry added.
