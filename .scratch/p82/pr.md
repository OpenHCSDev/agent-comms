## P82 — repaired source checkpoint

Head **50af0e4dc7479c1d7ca308c170f639c5227b0051**. Continues the specified 45a0a365d checkpoint and normally merges main.

Comparison base **154fa7ac9a1900aaa3a3174e5ef4cd71568a4767**:
- **src: 18 files +206/-270 = -64**
- **tools: 0**
- **tests: 12 files +54/-48 = +6**

### Owners and deleted decisions
- WakeDecision owns committed serialization and creation of an initial assignment (including the absence of an observer assignment). Publication invokes behavior; cohort acceptance, response validation and source coverage consume it. Deleted the external concrete-decision guards and `_decision_wire`.
- AssignmentState.completed derives from member-owned completion data; pending derives from the existing execution binding and declared successor relation. Completion polling no longer recovers member identity.
- Full-pending assignment owns selected-write eligibility. Full native validation invokes require_engagement rather than inspecting its concrete member. Triage reservation invokes reserve_triage; the existing transactional revision fence replaces the redundant discriminator-string CAS.
- Active-policy SQL derives from engagement declarations, not a PassiveWake exclusion. Registration binds only an explicitly declared policy: a subclass must not replace its inherited policy's initial state.
- Kept main's typed notification progress; removed FailedAssignment's duplicate FailedProgress wrapper.

### Six reported failures → zero
**109 passed, 0 failed** on the exact head, without new skip/xfail marks or weakened assertions:
```
python -m pytest tests/test_wake.py tests/test_coordination_nominal.py \
 tests/test_coordination_cohort.py tests/test_wake_candidate_index.py \
 tests/test_s3_legality_behavior.py tests/test_message_notifications.py \
 -q -o addopts='' --tb=short
```
- SentProgress owns the untimed pending label; its original sent time remains data. No label assertion changed.
- PreparingPhase now composes the existing LabeledActivity capability. Its phase label was previously discarded before presentation. No summary assertion changed.
- Restored the real private protocol marker for the native settlement fixture.
- Restored the response fixture to current ownership: route on obligation, original claim/link before that obligation, and the existing plural obligations collection. Both cardinality and nominal-state checks remain.
- **Bounded checkpoint reason:** one maintenance call is one bounded batch, so each independent test worker now consumes subsequent batches until the fixed committed hint is caught up; the final exact-checkpoint assertion remains, and worker outcome checks now require caught_up=True.

### Ratchet and source evidence
Full ratchet completed in one invocation with a 180-second allowance: **exit 0**, against **154fa7ac9**, no positive metric deltas or introduced violations:
```
python tools/debt_ratchet.py --root src --base 154fa7ac9 --head 50af0e4dc
```
The earlier completed run returned 1, exposing the remaining foreign absence probe and discriminator flattening; those competing decisions were removed, not suppressed. Earlier failed outputs remain in `.scratch/p82`.

Changed-source F401/F821 and whitespace checks pass. The requested member-dispatch grep returns **0**. NRA evidence covers Core, paired Toad and the audit dependency: initial census 1,064 modules/zero omissions; merged-source census 1,065/zero omissions, with declarations, inheritance, writes, imports and consumer sites retained. The expanded activity/label census is retained in `notification-owner-before.json`. Syntax does not prove arbitrary dynamic aliases.

Actual Comms/bus/SQLite operations and the restored native settlement boundary ran in these checks. This is not an installed native-provider/UI acceptance claim. Codex owns installation after review; that remaining live-path verification is explicitly retained.
