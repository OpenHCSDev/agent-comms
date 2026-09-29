# Current main367/377 integration, ready

Tested source: ce15a23d754d77b65946ccc13ba19e4ae3ea3918.
Integrated main: `4734433d5c9219666dc13a9aebf8a643a9cc30c3` (includes367,375,377).

Resolved the coordinated-runtime test conflict using367's TrackedTurnSession
module and the actual PrivateInputs.reserve method. The fsync injection now
patches that canonical owner directly. Selected execution enrollment and raw
admission use private_inputs.enroll/reserve/send_fence; selected summary and
native operation callers retain their respective owners. No wrappers or
compatibility imports. Corrected compaction_identity.require_source indentation
and method spacing; source digest validation is unchanged.

Noneditable installed package, shared immutable native-current-d3967e8b6ee0cf28.
Both PI_COMPACTION_TEST_PACKAGE and AC_NATIVE_COPIED_PACKAGE point to that package.
Serial pytest with -o addopts='' and owned .scratch/main367 basetemp:

- test_coordinated_runtime::test_private_raw_prewrite_fsync_unknown_never_dispatches_or_retries
- test_selected_input_lifetime_native::test_actual_saved_eof_preserves_unknown_and_never_replays
- test_selected_owner_compaction_integration::test_acp_selected_summary_handoff_uses_final_prompt_once[summary-unchanged-private]
- test_selected_owner_compaction_integration::test_correction_after_native_commit_never_mints_original_admission

**4 passed,33.81s.** Three actual native/ACP paths plus the real journal fsync fault
regression. The saved selected EOF case preserves UNKNOWN and old input records,
refuses automatic replay, then accepts one independent new input. The ACP case
asserts actual publication precedes the exact original binding and continues
through a second saved-session cycle. Postcommit correction refuses input authority.

Caller audit found no former journal consumer, including direct constructor and
class-bound calls. Ratchet no increase (-206 class excess,-2 foreign probes).
No unchanged broad matrix or CI wait. Parent owns merge/install/default.
Cleanup checked no process references and retired this run's owned env/scratch;
all prior and current receipts remain. No live data or shared native package edit.
