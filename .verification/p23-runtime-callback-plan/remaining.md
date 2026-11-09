# P23 callback contribution — qualified pinned composition

## Handoff

- Core baseline: `b47c7c0ded5005be39bfcf9e129297fabc4f127d`.
- NRA: `5981d592bd5297342ad8f1f129e6bfdd213906f0`; no NRA implementation edits.
- Expected prefix: contract-trace `guarded-closure-8.plan.json`, SHA256 `57a731682e4e6445671d55f3dc552ea91b488493c635bcf917275e77d0de5f82`. Its full v8 replaces v4/v6; do not apply them twice.
- Callback-only suffix: `callback-suffix.plan.json`, SHA256 `02226e2fa2f6671a95d2228f9f4ede626a39401562fc706205a7e9285f56d5a8`. **4 native stages, 57 operations; no duplicate roster/clock prefix.**
- Full pinned composition: `callback-plan.json`, SHA256 `6cdd87dbd67dcb6bbc5cdeb238867feee3e187bab864ed4d58d5df4f147a4a4e`.
- Sent to friction in `007cd875f198`; full qualification update `93ff4065c0f6`.

No product tree was applied. Friction owns application on its current headers, integrated qualification, final delivery, merge and install. The checks below qualify the pinned composition, not newer integrated source or installed behavior.

## Eight-row closure

Every requested module has zero targeted guards/local imports and passes fresh import/runtime annotation checks on the full v13 projection.

| Requested module | Actual provider/cut | Pinned qualification |
|---|---|---|
| backend.py | NativeSessionCallbacks / PromptSessionCallbacks on original NativeSession and TurnSession; original retained child uses concrete generic arguments | PASS; original NativeSession abstract, TurnSession concrete |
| execution/owned_turn.py | OwnedTurnRunner on actual TurnRunner; original TurnGoalAccount/TurnDelivery fields and turn_context operations preserved | PASS; original owned-turn consumers pass |
| execution/turn_inputs.py | Original InputForwarding inherits SessionInputsCallbacks; shared/prompt session capabilities stay distinct | PASS |
| execution/turn_output.py | Original TurnOutput inherits SessionOutputCallbacks; concrete failure/retry fields retained | PASS |
| prompt_state.py | Original prompt family inherits SessionPromptCallbacks | PASS |
| agent_activity.py | ActivityRegistration/ActivityRegistryRead on actual providers; actual observations implement RuntimeInfoProjection; original notice implements AssignmentNotice | PASS; original notification consumers pass |
| registry/thread_status.py | Original roster policy lowered once; PresenceThread/Entry/Snapshot and PresenceMessageSender on actual providers | PASS; original presence consumers pass |
| registry/registry_document.py | Actual snapshot/entry inherit read contracts; original delivery keeps frozen recipients; command callbacks use low TargetCommandBindings | PASS; both contextual TypeVar bounds resolve |

## Evidence

- `native-v13.log`, `native-cold-v13.exit`: full native replay clean, 61 projected files, no apply; exit 0.
- `cold-v13.log`, `cold-v10-results.json`: all eight fresh roots return 0 and resolve their local runtime annotations. The raw filename is historical; current contents are v13.
- `consumers-v13.log/.exit`: **58 collected, 58 passed, exit 0**, original turn-registry annotations, owned-turn goal replacement, thread status and message notification tests. No test source/assertion weakened.
- `command-binding-bounds-v13.log/.exit`: both changed command-binding generic bounds evaluate; RegistrySnapshot, RegistryEntry, Thread, MessageNotification, Delivered, SendResult and PersistentPiSession remain concrete; retained-session construction produces EmptyNative. Exit 0.
- `source-manifest.json`: exact pinned source hashes and NRA revision.
- `before-classes.csv`, `after-classes.csv`: original selected-module class census.
- `value-body-check-v9.json`: original dataclass field declarations checked intact; 679 original method bodies compared without imports. Only two taken_response bodies changed, both calling the actual notice's shared with_state operation. This is source preservation evidence, not a substitute for runtime checks.

## Composition / genuine shared sources

The suffix first relocates the **original** RecipientSelection to recipient_selection.py, then creates low callback declarations and small exact annotation/import patches. Actual provider inheritance uses native add/replace-class-base operations, not full-header replacement. Crossing bases are staged separately so same-target header edits do not overlap.

Shared provider headers: RegistryEntry, RegistrySnapshot, Thread, Registration, RegistryStore, Delivered, Message, CliCommand. Friction's stronger InputOrigin/acquisition work also touches registry providers; this contribution introduces no authority-cut/user-selection capability. Native base operations preserve existing header changes such as Container[str].

Shared callback annotations/support: registry_document.py; notification_progress.py / message_notification.py; messaging/sending.py; agent_events.py / runtime_info.py. Actual Message, ClaimMessage, notice and registry dataclass fields stay in their original declarations. AssignmentNotice borrows only recipient identity, state and required methods; PublishedMessageRead borrows reference/time from the actual Delivered. No wrapper or surrogate stored record is constructed. MessageNotification.with_state owns the original dataclass replacement used by both taken_response implementations.

Contract-trace owns RosterVisibility + DEFAULT_ROSTER and the clock/channel ancestry cut. Its v8 Thread/TaskOwner/read_basis operations precede this suffix. Use the callback-only file after the applied prefix; the full file already contains that prefix.

## Retained failures / limits

- `v9/` preserves the first full plan/projection and expanded simulation; its clock→Message→CommandTarget startup failure is in `cold-v9-results.json` and was corrected in peer v6.
- `v12/` plus `cold-v12.log` / `cold-v12-results.json` preserve the next channels→Thread bootstrap failures, corrected in peer v8.
- `roster-only-control/` retains the overly broad notice API import failure.
- `roster-only-control-v11/` retains the eager retention generic-binding failure.
- `roster-only-control-v12/` retains the independent control, including my invalid concrete-NativeSession assertion. The corrected check preserves original abstract prompt_type instead of changing the product.
- Earlier consumer error runs are retained. The first lacked projection stack/config, the next lacked package resources. The author now carries pinned resources/companion modules/config and reuses the existing prepared native pin. Those error runs are not passes.

P108/NRA #327 remains a separate claimed delivery awaiting its independent acceptance; this source contribution does not close it. The NRA routing rule is verified merged in Core #4478 (`86c6ffbd8`), for the next coordinated install, not a separate restart.
