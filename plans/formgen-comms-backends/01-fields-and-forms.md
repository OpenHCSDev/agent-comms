# 1. Shared fields and forms

**Planning status:** the input meanings and validation owner below are settled. The shared-parent extraction, complete class and caller list, Qt constructors and method lookup, and exact NRA steps still need completion before this whole phase is ready. This is not authorization to implement the backend.

## Outcome

Textual and Qt use the same field declarations, input meaning, validation and form-following algorithms. Native controls supply text/value input, feedback drawing, focus, painting, mounting and signal resources. An ordinary comms string does not become an inherited absence merely because its renderer is Qt.

Keep `DeclaredField`, `OptionalMemberField`, `FieldPath`, `DeclaredViews`, `FormPage` and ObjectState's existing topology. Do not create a second field schema or reconstruct nested paths. Both dated P142 repairs are already present; see [the current-source receipts](sources.md).

## 1.1 Input meanings: settled

The preservation target includes each original form's observable input semantics, not just identical initial widgets. These cases prevent merging the two current readers:

| Case | Literal/declared form | Inherited-value form | Current evidence |
|---|---|---|---|
| Empty ordinary string | The value is `""` | An empty inherited text field clears its raw override | Toad `forms/editors.py:143,173–174`; Qt `forms/widget_strategies.py:140–146`; Formbridge `parameter_form_service.py:360–367` |
| Text `"None"` | The value is the string | The current inherited conversion treats it as absence | Formbridge `parameter_form_service.py:312–314`; Toad's direct declared-type reader does not interpret this sentinel |
| Surrounding whitespace | Ordinary string input preserves it | Current `NoneAwareLineEdit` strips it | Same two text readers |
| Empty number | A rejected finished edit; restore the model value and show feedback | Clear the inherited override where the original form permits it | Toad `LineEditor.finish`; Qt `NoneAwareIntEdit._interpreted_text_value` |
| Incomplete number, for example `-` | Do not mutate the model; finishing rejects it | Keep the last committed value and do not emit a semantic commit | Qt `NoneAwareIntEdit:148–205`; Toad form refusal test |
| Unfinished container syntax | Not a successful value | Native Qt may show Intermediate; the model is unchanged | Qt `TypedLiteralValidator:365–383`, typed container/union readers |
| Complete wrong container/type/annotation bound | Reject before the state write | Reject before the state write | `DeclaredField.validate/edit`; Qt literal validation and form validation |
| Optional Set unchecked | Send the optional absence; preserve the member editor's declared type and feedback rules | Do not assume this is the same action as resetting an inherited override | Toad `OptionalEditor:273–313`; Qt optional creation/reset family |
| Programmatic paint | Show the model value without sending an edit back | Same | Toad native `prevent(...)` blocks; Qt signal/blocking boundaries |

**Decision:** literal and inherited input are separate nominal capabilities composed with the field kind. They are not a backend flag, a global conversion switch or a special case of `None` at every caller. Both backends must support the declared/literal family used by comms; existing OpenHCS inherited forms retain their own family and behavior.

`ParameterFormService.convert_value_to_type` is currently an inherited-value reader. Its global empty/string-sentinel handling cannot become the default for ordinary comms forms. Conversely, deleting that behavior globally would change existing Qt and bridge callers. Keep those callers on inherited semantics while factoring the common annotation/choice/enum/container conversion beneath both meanings.

### The bridge is a real caller, not a spare path

Formbridge `bridge/object_state.py:539–547` calls the same converter from `ObjectStateFieldMutationService._converted_value`; mutation at `:498–510` also has an authorization callback and its own accepted/rejected receipt. It must be in the migration and test set. Changing only widgets would leave the bridge answering a different question for the same inherited edit.

A converted None is not automatically a reset. The bridge's reset request calls `state.reset_parameter`; an ordinary request calls `state.update_parameter` with the converted value, even when that value is None. The result also preserves the original reset flag. Keep those calls, authorization order and receipts distinct. Do not implement the inherited reader by mapping every empty/None result to reset merely because one current displayed value happens to agree (paper1 Theorem 4.8).

## 1.2 Determining owners

| Required fact | Owner retained | What is removed from consumers |
|---|---|---|
| Field identity/path, annotation, label, help, default, constraints, optional member | `formbridge.forms.declared_fields.DeclaredField` and original ObjectState declarations | Hand-copied field metadata and second path/type reconstruction |
| Annotation acceptance before a declared edit | `DeclaredField.validate`, followed by its edit/write contract | Toad `LineEditor.acceptable`'s independent `validate_annotation_value(...)` call |
| Literal versus inherited reading | The shared form/input family, with the separate cases above | Backend-wide empty-string/`"None"` coercion used for literal fields; per-widget interpretation of these domain meanings |
| Object value, resolved changes, history and dirty fields | Original ObjectState | New form value caches, dirty-state copies and direct private fixture setup |
| Offered choice meanings/labels | `AnnotationChoices` and the original declared enum | Parallel option rosters or string-to-kind maps |
| Native validation state and events | Qt validator/signals or Textual Input events | Nothing: Intermediate and native event ordering are separate renderer facts |
| Native visual policy | Original controls, theme and layout declarations | Nothing: terminal clipping, Qt reset/provenance chrome and lazy mounting are not shared value semantics |

The edit still requires the original authorization before mutation. Reset, live invalidation deferral, sibling refresh and root/cross-window notifications keep their existing order. Sharing a validator does not authorize bypassing that sequence.

### Field-extraction metadata found by the full scan

Qt `form_init_service.py:65–86` declares four `ExtractedParameters` fields using its own `computed`, `project` and `initial_values` metadata; `_extract_parameters` at `:511–531` interprets those keys. Keep the dataclass as the result shape and replace this internal little metadata language at its owner:

- Add `formbridge/forms/field_extraction.py: FieldExtraction[T]` with `extract(source) -> T`.
- `ProjectedExtraction` owns per-parameter projection into a result mapping; `InitialValuesExtraction(ProjectedExtraction)` adds the original initial-values override; `ComputedExtraction` owns direct source computation.
- Attach one typed extraction rule under the `FieldExtraction` type key in each dataclass field's metadata. The builder calls that rule; it does not branch on three strings or keep a second field roster.
- Map `default_value` to initial-values projection, `param_type` and `description` to their original projections, and `object_instance` to direct computation. Keep each original callback, DottedFieldPath use and output type/order.
- Do not merge this extraction result with ObjectState or another configuration record. It is a separate form-build result, not the value/history owner.

This is a deliberate type/owner change with no automatic NRA recipe reported. The exact declaration/method move and tests for defaults, exclusions, nested descriptions and computed fields must be in the final phase steps. The other two scan findings do not justify changing the unrelated Core family or splitting the form-build readiness condition; see [their source checks](sources.md).

## 1.3 Shared implementation to extract

The following public parents are the destination design, not existing APIs. Implementations move from the original source; do not write an independent second algorithm from this prose.

| Destination in Formbridge | Original algorithm | Native obligations left abstract |
|---|---|---|
| `forms/value_editing.py: ValueEditing` | Toad `ValueEditor.value_text/default_note/send/refuse`, default `finish` | Paint the committed value, show/clear feedback, and native control construction/binding |
| `forms/value_editing.py: LineEditing(ValueEditing)` | Toad `LineEditor.read/acceptable/finish` | Read/set native text; decide which native event requests finish. Acceptance calls the bound field's validation owner |
| Same module: optional/choice/enum/multiline editing capabilities | `OptionalEditor`'s held/member rules; enum/choice labels; multiline default-note decision | Native checkbox/member visibility, native offered-option projection, TextArea/QTextEdit events and feedback delivery |
| `forms/form_following.py: FollowingForm` | Toad `FormView.values_changed/marks_changed/finish/filter`, subscription attach/detach | Index the actual native field views; paint/mark/show them; mount/reveal and native destruction |
| Shared annotation-conversion provider beneath literal/inherited families | Current Formbridge conversion methods plus Qt's pure literal parse/projection routines | Qt `QValidator.State`, `QLineEdit.text`, cursor position, timers and signal connections stay native |

`ValueEditing` holds the bound field, not a copied value. Feedback and pending text must each have one owner: extracting a generic string attribute beside Textual's reactive `feedback`, or a second committed-value cache beside `NoneAwareIntEdit`, is not this move.

`FollowingForm` follows the original ObjectState and actual field-view index. Its change work is proportional to the affected visible paths, not a full rebuild or repaint. Subscriptions attach once, detach before the native view disappears, and late callbacks cannot act on a replacement view.

### Selected Textual editor bases and constructor order

Keep the existing native names in `toad/forms/editors.py`; the following are the intended bases after extraction, not classes claimed implemented here. Shared editing parents in `formbridge/forms/value_editing.py` own the original pure bodies; native classes keep controls, events, styling and native value/feedback access.

| Native class | Intended bases / own declaration | Important winner |
|---|---|---|
| `ValueEditor` | `ValueEditing, DeclaredViews, Widget`, with native `FormEditorMeta(type(FormInput), ABCMeta)` | Native constructor binds field before native control initialization; shared send/refuse/value text; native feedback and paint hooks |
| `BoolEditor` | `ValueEditor, FormCheckbox`; original `shows=(bool,)` | Native checkbox events/paint; shared send and validation |
| `LineEditor` | `LineEditing, ValueEditor, FormInput` | Shared read/acceptable/finish; native text access, control arguments/events and paint |
| `IntEditor`, `FloatEditor`, `StrEditor` | Original `LineEditor` parent and each original shows declaration | Keep the native InputType setting; do not repeat parse/finish bodies |
| `MultilineEditor` | `MultilineEditing, ValueEditor, UntintedTonePaint, FramedControl, TextArea`; original Multiline declaration | Shared empty default-note policy; native TextArea event/paint/geometry |
| `SelectEditor` | `SelectEditing, ValueEditor, WidgetView, Select[object]` | Shared label interpretation; native offered-value/NULL boundary, option installation, focus and paint |
| `ChoicesEditor` | `ChoicesEditing, SelectEditor`; original AnnotationChoices declaration | Shared choices/options projection; native option refresh during paint |
| `EnumEditor` | `EnumEditing, SelectEditor`; original Enum declaration | Original enum/label generator on the shared parent |
| `OptionalEditor` | `OptionalEditing, ValueEditor, WidgetView, containers.VerticalGroup`; original Optional declaration | Shared member/held-value algorithm; native checkbox, container and silent feedback operations |

Delete the current `if TYPE_CHECKING: EditorWidget = Widget; else: EditorWidget = object` workaround and its alias. The native ValueEditor is a real Widget; Formbridge's ValueEditing is not. Keep DeclaredViews before the actual native widget chain so it consumes shows first. Preserve each class's own shows declaration rather than inheriting the parent's roster. The native form metaclass combines the actual Textual metaclass with ABCMeta; it does not copy abstract-method flags by hand. Verify the actual MRO, abstract enforcement, focus and widget metadata in the implementation checks. Current Widget.__init_subclass__ only assigns can_focus when explicitly supplied; do not add a blanket focus override or claim a prototype proves the finished classes.

Retain the native constructor sequence: bind the one original DeclaredField; compute the control arguments from it; initialize the actual control once through the MRO; bind native events afterward. Shared editing parents do not start another native constructor or store a second committed value. Their field annotation lives once on the shared parent. Numeric/choice/optional member construction still receives the original narrowed DeclaredField/OptionalMemberField.

The shared primitive signatures are `display_feedback(text: str) -> None`, `clear_feedback() -> None`, `read_text() -> str` for line controls, and `displayed_options() -> Iterable[tuple[str, object]]` plus `lists(value: object) -> bool` for selects. `clear_feedback` is deliberately silent: OptionalEditor currently uses set_reactive on the hidden member; turning that into a later feedback event could overwrite the parent's newer refusal. Shared LineEditing.finish reads through read_text; its acceptable calls the existing field.validate. Shared SelectEditing.value_text reads the actual installed native options rather than keeping another option cache. Native callbacks decode their foreign event/NULL marker once and call the shared operation.

OptionalEditing retains the actual member editor as ValueEditing, not another value record. Its native primitives are `set_member_shown(held: bool) -> None` and `paint_held(held: bool) -> None`; the latter changes the native checkbox without echo. Native held_changed decodes the checkbox event and calls shared `change_held(held: bool)`, retaining the original silent-member-clear/send-None sequence. Model None, optional membership and inherited fallback remain the different meanings in §1.1; this declared/literal matrix does not authorize sending an inherited reset through the wrong field contract.

### Remaining native composition

- Textual retains the `toad.forms.editors` and `toad.forms.form_view` native modules. Moving a name does not justify leaving a re-export shim.
- The native `ValueEditor` keeps `DeclaredViews` before its widget base so `shows=` is consumed before Textual sees it. Preserve the intentional non-inheritance of `DeclaredViews.shows` from parent to child.
- A boolean editor composes value editing and the checkbox; a line editor composes line editing and native input; an optional editor composes held/member behavior and a native container. A multiline editor keeps its framing/paint capabilities. These crossings stay parents, not constructor flags.
- Qt must compose the same literal/inherited semantic parents with its real controls. Do not call the current inherited `ParameterFormManager` a declared comms form without changing its input family.
- **Still to finish:** exact Qt concrete class/base lists, their constructor order and the generic mutation/notification hook boundary. Existing `ParameterFormManager`, `WidgetCreationConfig` and its regular/nested/inline/optional members must be included, not wrapped behind a new unrelated Qt form application.
- For each composed class, check the actual runtime MRO and method winner for initialization, field validation, read/finish, model write, paint and close. Source simulation alone does not execute Qt/Textual metaclasses.

## 1.4 Callers that change

These are required caller sets from the current source search. The NRA class and import map still has to check these bindings and include other callers before a ready claim.

### Toad

- `src/toad/forms/editors.py`: all value/line/bool/int/float/string/multiline/select/choices/enum/optional editors. Remove the shared bodies and direct validation import after their parents supply them; keep native events and presentation.
- `src/toad/forms/form_view.py`: FieldView/ValueView/GroupView, FormView and InlineFormView. Preserve bounds/help markup as a native projection of the same field; preserve the native choice registry and page order.
- `src/toad/screens/settings.py`: whole/page forms, search, pending-edit finish before save/apply and native setting effects.
- `src/toad/widgets/comms_command_dialog.py`: command form creation, finish/validation and permission/confirmation behavior. Do not make its command context a Formbridge dependency.
- `tests/textual/test_forms.py`, `tests/textual/test_settings_screen.py` and the command-pane/context tests listed in the source map.

### Formbridge

- `src/formbridge/forms/declared_fields.py`: remains the declaration/validation authority; extend at this owner only where the required behavior needs it.
- `src/formbridge/forms/parameter_form_service.py`: factor its conversion meanings without changing metadata-only `parameters_for_state` readers into inherited editors.
- `src/formbridge/bridge/object_state.py`: keep authorization, reset semantics and exact mutation receipts, and migrate its conversion call to the original inherited meaning.
- Existing declaration/view/bridge/conversion tests; new shared-family behavioral cases below.

### Qt

- `src/pyqt_reactive/forms/widget_strategies.py`: `NoneAwareTextValueMixin`, `NoneAwareLineEdit`, `NoneAwareIntEdit`, `TypedLiteralContainerEdit`, `TypedLiteralUnionEdit`, their pure parsers and native validators, `DebouncedTextChangeEmitter`, creation/conversion/signal/value-assignment owners.
- `src/pyqt_reactive/forms/parameter_form_manager.py`: service setup, `_convert_widget_value`, `update_parameter`, nested shared-state construction, attach/detach callbacks and disposal.
- `src/pyqt_reactive/forms/widget_creation_config.py`, `widget_creation_types.py`, `form_init_service.py`, `form_value_validation.py` and `parameter_form_tree_index.py`: field construction, accepted input type, read-only state, transaction cleanup and actual field indexing.
- `src/pyqt_reactive/services/field_change_dispatcher.py`: retain the single DottedFieldPath, authorization/write/sync/invalidation/sibling/signal order; remove only the behavior that is genuinely supplied by the new shared owner.
- Existing OpenHCS form consumers must retain their inherited meaning. Search its registered production roots before claiming every caller is covered; do not rewrite its submodule gitlinks as part of source investigation.

## 1.5 NRA work and explicit limits

Read the complete class list and imported parents before approving the final class move. Include conditional/local/ambiguous declarations as OPEN rather than inventing resolved native ancestors.

The native NRA CLI exposes source-index/target selectors, preflight, ordered plan simulation, projected findings/source indexes and revision-checked application. Those are available mechanisms, **not a promise that this extraction has a recipe**. The operation-specific constructors and proof scope must be read before filling this section with exact stages.

Unresolved tool obligations:

1. Select original methods/classes and move shared implementations to the named destination parents without replacing them with independently authored bodies.
2. Migrate imports/callers across the owned packages; use the complete import graph for guarded-import promotion. Existing guards/local imports in touched modules must not simply be unguarded with a generic patch.
3. Prove or explicitly refuse the changed Qt/Textual constructor and MRO relations, including their native metaclasses. Refusal is a tool gap to implement with NRA's existing owners, not permission for an unproved shell rewrite.
4. Separate unchanged structural moves from the intentional split of literal/inherited reading. The semantic decision is established by the conflicting input cases, not by a clean parse or `is_clean` flag.

## 1.6 Checks and finish

Existing protections to retain:

- Toad `tests/textual/test_forms.py`: declaration-to-editor selection, edits for every kind, refusal/model restoration, dirty marks, external changes without echo, case-insensitive nested filtering, and imports without Qt/Core/zmq/numpy.
- Toad settings and command tests: finish the focused editor before collecting values; retain command availability/confirmation and setting-effect ownership.
- Formbridge declaration/view/toolkit-import tests and bridge tests: one annotation/metadata owner, view-family selection, exact mutate/reset receipt and authorization.
- Qt annotation-choice, field mutation authorization, form build transaction, form tree index and `test_widgets.py` cases. Keep `test_nested_dispatch_uses_one_state_address_for_mutation_and_signals` as protection of the already-correct path, not new path-repair work.

Add one shared-family test matrix for the conflicting values in §1.1, with native cases only for events/painting/validators. Exercise both literal and inherited forms with the same backend; a Textual-versus-Qt comparison alone would miss the missing domain distinction. Include a new scalar/choice field and show that only its declaration and irreducible native rendering need additions, with no consumer kind switches or rosters.

UI checks: pending invalid input, optional member feedback, save while focused, reset/undo, replaced/disposed form, model changes during staged mount, no signal echo and no orphan subscription/timer. The generic validation must execute before the state mutation and every authorized write must retain its original notification/transaction ordering.
