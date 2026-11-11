# 1. Shared fields and forms

**Planning status:** input meanings, validation, Qt editor/form bases, the field/write boundary and the retained manager/build lifecycle are selected below. The Qt field-view/model-following map, inherited paint separation, inline admission, complete caller/import graph and exact NRA stages still need completion. Native construction/event/teardown checks remain implementation work. This is not authorization to implement the backend.

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
| `forms/value_editing.py: ValueEditing` | Toad `ValueEditor.value_text/default_note/send/refuse`, default `finish` | Paint the committed value and show/clear feedback |
| Same module: `ControlConstruction(ValueEditing)` | Toad `ValueEditor.__init__`'s bind-before-native-init/bind-events-after sequence and its default-empty control_arguments | Abstract native bind_control; default-empty configure_control permits native setters after initialization. Accept field plus native keyword arguments alongside control_arguments, with duplicate arguments rejected by Python rather than silently overriding a field-derived value. Keep no second argument/value cache. |
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
| `ValueEditor` | `ControlConstruction, DeclaredViews, Widget`, with native `FormEditorMeta(type(FormInput), ABCMeta)` | Native constructor binds field before native control initialization; shared send/refuse/value text; native feedback and paint hooks |
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

### Qt field binding and the single write boundary

The shared editor calls `field.edit(value)` once. Do not call the current `DeclaredField.edit` and then notify Qt: `DeclaredField.edit` already writes ObjectState (`declared_fields.py:247–251`), so that sequence would write twice and perform the first write before Qt authorization.

Add these native declarations in `pyqt_reactive/forms/declared_fields.py`. These are selected implementation targets, not APIs claimed to exist today:

| Declaration | Bases and constructor | Own behavior |
|---|---|---|
| `QtDeclaredField` | `DeclaredField`; `__init__(manager: ParameterFormManagerABC, path: DottedFieldPath)` | Retain this view's manager and initialize the original field with `manager.state` and the original path. Inherit declaration, annotation, metadata, value, default and validation. `edit(value: object) -> None` validates through the inherited owner, then calls `manager.commit_field_value(path.field_name, value)`; it does not write ObjectState itself or pass a literal value through the inherited converter. |
| `QtOptionalMemberField` | `OptionalMemberField, QtDeclaredField`; inherit the same constructor | The original OptionalMemberField supplies its narrowed annotation; QtDeclaredField supplies edit. No copied optional declaration or separate value. |
| `QtDeclaredField.member` | An override of the existing property | Return `QtOptionalMemberField(manager, path)`. The base property returns a plain OptionalMemberField and would otherwise lose the Qt authorization/notification path. |

Extend the existing `widget_creation_types.ParameterFormManager` contract with `state: ObjectState` and abstract `commit_field_value(param_name: str, value: ParameterValue) -> None`; import ObjectState and the existing ParameterValue contract there at runtime. QtDeclaredField imports that original ABC as ParameterFormManagerABC, never the concrete parameter_form_manager module. The dependency is concrete manager → native creation/editor → Qt declared field → original manager contract → ObjectState/Formbridge. The contract imports none of those consumers, so this binding does not require a TYPE_CHECKING or local import. The selected native creation module constructs the field after it has the manager and its original DottedFieldPath. An optional field's unchecked operation uses the outer field; the member editor uses the narrower QtOptionalMemberField, so None is not incorrectly validated against the member annotation.

Add `ParameterFormManager.commit_field_value(param_name: str, value: ParameterValue) -> None` at the existing Qt manager. It does not parse again: inherited callers retain their original conversion/validation behavior, while QtDeclaredField validates its typed literal before calling. Move the original atomic/FieldChangeEvent/dispatch block out of `update_parameter` (`parameter_form_manager.py:859–881`) into this method, unchanged in ordering. Call it from:

1. `update_parameter`, after its existing inherited conversion and native widget assignment;
2. `WidgetCreationPipeline.on_widget_change`, after `_convert_widget_value`, replacing its second atomic/dispatch block (`widget_creation_config.py:1068–1082`);
3. `QtDeclaredField.edit`, after declared-field validation, with no inherited empty/sentinel conversion.

The first two callers remain inherited-value entry points; the third is the declared/literal entry point. This is not a boolean mode or a callback to a second writer. Each sends one original FieldChangeEvent through the existing manager, with the same root, source and atomic scope. Remove the two old dispatch-block copies when the owner method replaces them (IMPL-12; paper1 Theorem 4.6). This extraction has no executable NRA recipe in the retained scan; its member/call-site rewrite remains an explicit tool gap until preflight proves the exact move.

Retain this actual dispatcher order (`field_change_dispatcher.py:352–389`): enter the source guard; reject a reset-blocked echo; obtain one FieldDispatchContext/DottedFieldPath; run SourceStateUpdateStage; sibling placeholder refresh; live-context notification; enabled styling; root/cross-window signals; release the guard in finally. SourceStateUpdateStage (`:99–131`) calls `before_mutation` before ObjectState, defers live invalidations for an ordinary edit, then synchronizes the source widget. Keep failure-time invalidation flushing and re-raise. Do not move a validation or write behind the signal stage. Reset retains its original separate reset operation and receipt; this new ordinary-edit method does not redefine it.

### Retained Qt construction and lifecycle

Keep the existing native form/build family rather than building an unrelated Qt form application around the shared editor:

| Existing declaration | Bases / signature retained | Owner of the native work |
|---|---|---|
| `widget_creation_types.ParameterFormManager` | `ABC`, presently `metaclass=_CombinedMeta` | Required manager methods, including update/reset, nested creation, ownership and build-completion registration; this is not a QWidget or another state store. |
| `parameter_form_manager.ParameterFormManager` | `QWidget, ParameterFormManagerABC, FlashMixin, ConfigTreeFlashManager`; `__init__(state: ObjectState, config: FormManagerConfig \| None = None)` | The one native form and existing chrome/tree/build owners. Do not pass a copied ObjectState into an added form wrapper. |
| `WidgetCreationConfig` | `ABC`, `metaclass=AutoRegisterMeta` | The original ParameterInfo-MRO registry and create/layout/store/reset/structure methods. This non-widget family does not need a SIP metaclass. |
| `RegularWidgetCreation` | `WidgetCreationConfig`, registered for `ParameterInfo` | Responsive row, actual editor creation, field label/help, reset button, signal connection and read-only native policy. |
| `NestedWidgetCreation` | `WidgetCreationConfig`, registered for `DirectDataclassInfo` | Existing nested form, group chrome, layout and help/provenance behavior. |
| `InlineWidgetCreation` | `NestedWidgetCreation`, registered for `InlineDataclassWidgetInfo` | Only the existing inline container/editor differences. |
| `OptionalNestedWidgetCreation` | `NestedWidgetCreation`, registered for `OptionalDataclassInfo` | Existing optional title/presence controls and nested lifecycle; it is not the scalar OptionalMemberField. |
| `WidgetCreationPipeline` | Existing native coordinator; `__init__(manager: ParameterFormManager, param_info: ParameterInfo)` and `run() -> QWidget` | Select creation behavior once, then container → content-update context → configure/layout/title → main widget → placement/reset → structure connection → store/connect editor → label styling. Share its mutation block through commit_field_value, not through another pipeline. |

Preserve the concrete manager constructor sequence from `parameter_form_manager.py:314–600`: resolve configuration and the canonical field_id/target; initialize QWidget once with the native parent; bind the original state and parent manager; share the root FormBuildTransaction with nested managers; derive scoped parameters/types from that state; initialize the actual widget/tree/chrome indexes and view flags; install the existing services/creation owner and root visual-update state; build the UI; connect native signals; subscribe the root to resolved/materialized state changes; only then finish initial load. Do not dispatch an edit while these indexes or authorization fields are unfinished.

Nested creation at `parameter_form_manager.py:727–760` retains the same state, canonical nested field_id, parent manager, visual scope, function target and before_mutation callback. A declared-editor family must propagate through this creation owner; leaving the hard-coded inherited constructor at `:746` unchanged would silently change input meaning inside a declared form. The exact literal/inherited native subclass selection remains listed below as unfinished, rather than pretending the current constructor already propagates it.

Disposal stays at the existing `dispose() -> None` (`:1027–1045`): mark the manager disposed; cancel root deferred construction; disconnect registry/root state subscriptions; release the existing hierarchy registration. Late deferred work must be rejected by the existing build transaction before it touches a replaced widget. No per-editor copy of the form subscription is added.

### Qt metaclass owner

The current form contract uses `_CombinedMeta(ABCMeta, type(QWidget))` at `widget_creation_types.py:70`, while native controls use `PyQtWidgetMeta(type(QObject), ABCMeta)` declared inside `protocols/widget_adapters.py:40`; the no-Qt branch replaces the latter with ABCMeta. These are different current declarations, not proof that a proposed combination runs.

Use the existing native owner `pyqt_reactive.protocols.widget_adapters.PyQtWidgetMeta`, retaining its QObject/SIP-before-ABC order. The form contract and concrete manager import it directly and remove their `_CombinedMeta` declaration/import. These native modules already require PyQt6; the no-Qt fallback is not a working native metaclass and is not used to certify construction. No extra metaclass module or replacement implementation is needed. Do not make Formbridge or its shared editing parents import this Qt owner.

The same native metaclass copy occurs in `widgets/status_indicator.py:19`, `widgets/shared/abstract_manager_widget.py:94` and `widgets/shared/zmq_server_browser_widget.py:49`; migrate those native ABCs with the form contract rather than keeping extra answers to the same native capability. Existing PyQtWidgetMeta users are widget_strategies, no_scroll_spinbox, enhanced_path_widget, sectioned_dataclass_editor, clickable_help_components and widget_adapters' controls; tests include test_inline_dataclass_chrome and test_scoped_table_widget. Direct those imports to the existing owner, check the complete sibling import graph, then remove only the protocols package's PyQtWidgetMeta re-export. Do not move these control bodies or change their identities merely to import their metaclass.

This is a selected owner/move, not a verified native metaclass repair. Implementation must instantiate the real controls and manager on the declared Qt stack, reject an incomplete ABC, and check actual method winners and destruction. A static C3 calculation cannot prove SIP construction or abstract enforcement. The retained NRA report supplies no proof for changing the metaclass order of the form contract; record that bounded gap rather than claiming a codemod or runtime result.

### Selected Qt editor family

Add the native family in `pyqt_reactive/forms/declared_editors.py`. It covers the existing declared/Textual value kinds below; it is not permission to send a previously unsupported annotation to an inherited editor. The following are selected target declarations, not implemented classes:

| Declaration | Bases / own declaration | Native work and lookup winner |
|---|---|---|
| `QtEditorSignals` | A plain Python capability, not QObject/QWidget | Declare `feedback_changed = pyqtSignal(str)` once. Concrete controls put this capability before their actual Qt control base. |
| `QtValueEditor` | `ValueEditing, DeclaredViews, ValueGettable, CurrentValueValidatable, QWidget`, `metaclass=PyQtWidgetMeta` | The typed QWidget/editor role; do not put the signal declaration here. Declare the required feedback_changed property abstract; QtEditorSignals supplies it on concrete controls. Own Qt feedback display/clear, the native feedback property, declared_for, get_value and validate_current_value. Leave paint/configure_control/bind_control to native members. |
| `QtBoolEditor` | `ControlConstruction, QtEditorSignals, QCheckBox, QtValueEditor`; `shows=(bool,)` | Initialize/paint checked state without echo; bind toggled to shared send. The real QCheckBox is before the common QWidget role. |
| `QtLineEditor` | `LineEditing, ControlConstruction, QtEditorSignals, QLineEdit, QtValueEditor` | Shared read/acceptable/finish; native read_text returns text(), paint uses value_text and blocked setText, editingFinished calls finish. No second parse or write in a Qt slot. |
| `QtIntEditor`, `QtFloatEditor`, `QtStrEditor` | `QtLineEditor`; each original int/float/str shows declaration | Empty bodies apart from their declaration. The shared annotation reader supplies conversion; str keeps empty text, whitespace and the literal None string. |
| `QtMultilineEditor` | `MultilineEditing, ControlConstruction, QtEditorSignals, QTextEdit, QtValueEditor`; original Multiline declaration | Set acceptRichText false; use toPlainText/setPlainText. Native textChanged sends the actual text; blocked paint only replaces different text. Shared multiline default-note policy remains on its parent. |
| `QtSelectEditor` | `SelectEditing, ControlConstruction, QtEditorSignals, QComboBox, QtValueEditor` | Native item text/data are the one installed option list. displayed_options and lists read that list. Paint with blocked signals; an unlisted value uses index -1 plus the original value text as placeholder, not an invented choice. activated(index) decodes the native index once and sends its item data. |
| `QtChoicesEditor`, `QtEnumEditor` | Respectively `ChoicesEditing, QtSelectEditor` and `EnumEditing, QtSelectEditor`; original shows declarations | Original shared option/label generators. Choices repaint refreshes the installed native options before the common selection paint; no parallel option cache. |
| `QtOptionalEditor` | `OptionalEditing, ControlConstruction, QtEditorSignals, QtValueEditor`; original Optional declaration | The common role is already a QWidget, so no second QWidget base. configure_control creates its Set checkbox, layout and member from field.member, parented to this control. Native toggled calls change_held; member feedback_changed calls display_feedback. Shared held/member/finish behavior remains on OptionalEditing. |

Every concrete Qt class that combines independent bases explicitly selects PyQtWidgetMeta. Subclasses with only one native editor parent inherit that metaclass. DeclaredViews owns shows and consumes the keyword before the framework; its lookup is rooted at QtValueEditor, not the Textual ValueEditor tree.

ControlConstruction owns `__init__(field: DeclaredField, **native_arguments: object)`: bind the original field; initialize the native control once with the original control arguments and supplied native arguments; configure_control; bind_control last. Textual's configure_control is empty, retaining its original constructor effects. Qt callers supply parent as a native keyword; Qt configure_control performs initial blocked paint before connecting user events. The construction provider precedes the actual Qt control, while the actual control precedes QtValueEditor's common QWidget branch. Do not add per-leaf forwarding constructors or call QWidget.__init__ again from the role.

Literal line controls do not install the inherited integer validator. A QLineEdit validator can suppress editingFinished for invalid/intermediate text, which would prevent the shared finish/refusal/restore path. Let pending text remain native and commit only through shared finish on Enter, blur or explicit form finish. Inherited NoneAwareIntEdit and its validator/last-committed-value behavior remain unchanged.

QtValueEditor's feedback property reads the native tooltip, not a second feedback string. display_feedback sets that tooltip and emits feedback_changed, including an empty text after a successful send. clear_feedback empties it silently; OptionalEditing uses that silent operation when hiding the member, so an old member notification cannot overwrite a newer parent refusal. get_value returns the bound field's committed value; validate_current_value finishes pending input, then raises ValueError if feedback remains. It must not silently pass after an authorization or parse refusal.

QtValueEditor deliberately does **not** declare ValueSettable. That contract promises assignment of its argument (`widget_protocols.py:92–107`); repainting from field.value is a different operation. Ordinary model updates must reach paint through the shared form-following owner. The exact field-view index and the removal of inherited raw/placeholder painting from this declared path still need completion below. Do not add a set_value method that ignores its argument merely to enter the old refresh loop.

### Factory, conversion and signal entry points

Add these methods to the existing native manager contract and its concrete implementation:

- `create_field_widget(ctx: WidgetBuildContext) -> QWidget`: move RegularWidgetCreation's actual `_widget_creator(...)` call into this owner. The inherited implementation calls the existing create_pyqt6_widget with the same five arguments. RegularWidgetCreation calls this method; delete the mutable `_widget_creator` assignment at parameter_form_manager.py:552 and its only regular-field read at widget_creation_config.py:526.
- `bind_field_widget(widget: QWidget, param_name: str, on_change: WidgetChangeCallback) -> None`: move the current ordinary store_widget signal connection and read-only policy into this manager method. WidgetCreationConfig.store_widget still indexes the actual control once, then invokes this hook. The inherited method retains PyQt6WidgetEnhancer and WidgetService behavior.
- `convert_field_value(value: ParameterValue, param_name: str) -> ParameterValue`: own the original inherited service conversion once. update_parameter and _convert_widget_value call it instead of repeating the service call. The existing native Path boundary conversion remains before it in _convert_widget_value.

Select `DeclaredParameterFormManager(FollowingForm, ParameterFormManager)`, with the same inherited `(state, config)` constructor and explicit PyQtWidgetMeta. Its create_field_widget binds `QtDeclaredField(self, self._object_state_path_for_field(ctx.param_info.name))`, selects QtValueEditor.declared_for and retains the existing widget objectName. Its bind_field_widget does **not** connect WidgetCreationPipeline.on_widget_change: the editor's constructor already connects native user events to the shared field edit. It applies read-only to the whole native control subtree when requested. Its convert_field_value validates the already typed value through the original bound-field validator and returns it without inherited empty/sentinel coercion. No second ParameterFormService or backend-mode flag is added.

Change the one nested-manager construction at parameter_form_manager.py:746 to construct the current form family with the same state/config. Both selected families inherit the original constructor, with no added arguments. Before applying that move, NRA must verify constructor substitution for all resolved subclasses; conditional/aliased/unresolved subclasses remain OPEN. Do not broaden this to arbitrary type(self) constructors without that proof. Root and nested declared forms must retain the same field reader, writer and event binding.

NestedWidgetCreation already stores a nested form without adding a scalar change callback. InlineWidgetCreation is different: its store_widget at :764–768 connects the registered inline container's actual change signal. Do not globally disable that connection or treat its custom factory as a QtValueEditor. Its literal-input admission and the declared form's model-following/paint contract still require completion; until then no fallback to the inherited reader is permitted for an unsupported inline declaration.

The signal-shape evidence is deliberately limited. An existing Python 3.12/PyQt 6.11 environment can create the explicit-metaclass class and register feedback_changed from a pure capability before QLineEdit. Putting that signal only on a later QWidget branch loses it (`indexOfSignal` returns -1), even though the Python MRO is valid. A separate signal capability before QLineEdit preserves feedback_changed, editingFinished and textChanged and satisfies the role's abstract property in the metadata check. No QWidget/QApplication was instantiated, and no Python-3.14 constructor, event delivery, parenting or teardown result is claimed. The retained failed and passing experiments are listed in sources.md.

### Remaining native composition

- Textual retains the `toad.forms.editors` and `toad.forms.form_view` native modules. Moving a name does not justify leaving a re-export shim.
- The native `ValueEditor` keeps `DeclaredViews` before its widget base so `shows=` is consumed before Textual sees it. Preserve the intentional non-inheritance of `DeclaredViews.shows` from parent to child.
- A boolean editor composes value editing and the checkbox; a line editor composes line editing and native input; an optional editor composes held/member behavior and a native container. A multiline editor keeps its framing/paint capabilities. These crossings stay parents, not constructor flags.
- Qt must compose the same literal/inherited semantic parents with its real controls. Do not call the current inherited `ParameterFormManager` a declared comms form without changing its input family.
- **Still to finish:** the exact Qt field-view index/model-following hooks, complete inherited raw/placeholder-paint separation, inline-factory literal admission, and constructor/caller/NRA proofs for the selected editor/form families. The bases, ordinary factory/conversion/signal hooks and nested-family requirement are selected above, not implemented or called runtime-verified.
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

The operation-source check at NRA `5af050d8ed4855f8351751ef7236a31cf914705a` bounds the new Qt steps more precisely. This is an API/proof-scope read, not a completed simulation or permission to apply a plan:

| Move | Existing NRA owner and limit |
|---|---|
| Promote original whole editor methods to their selected shared parent | `PromoteClassMembersToAncestorOperation` (`class_member_authority_codemod.py:1645`) takes a source class target, a destination SourceRewriteTarget and member_names. It requires an existing nominal ancestor. Declare/reindex that ancestor first; the current tree does not contain ValueEditing/LineEditing. Do not claim this operation creates those parents or certifies a changed method body. |
| Extract ControlConstruction and add native keyword/configure hooks | The original constructor is a direct method, but its new signature/hook is an intentional change, not an unchanged member move. Whole-member promotion alone does not prove native initialization, default arguments or hook ordering. Keep original-body movement separate from the added native contract and record the constructor-proof gap; the metadata experiment does not close it. |
| Fold the two Qt atomic/dispatch blocks into commit_field_value | Whole-member promotion is not this move: the blocks are inside update_parameter and on_widget_change, and the latter belongs to an unrelated native coordinator. `PromoteExactLeafMethodsToAncestorOperation` (`:1234`) also requires its proved exact leaf-method component. The retained scan supplies none. Fragment extraction plus manager-receiver/call migration remains a specific operation gap; do not copy an authored body and call it a proved promotion. |
| Change a native metaclass or introduce QtDeclaredField's different writer | `ClassBaseMutationOperationABC` (`codemod.py:1058`) concerns direct bases; it does not establish equivalence of a metaclass keyword or the new authorization/dispatch policy. `PatchTargetOperation` (`:1091`) can select an exact source patch, but that is source targeting, not native behavioral proof. Keep the selected design and its native checks; no admitted automatic recipe is claimed. |
| Migrate imports after a proved declaration move | `EnsureImportOperation` (`codemod.py:2516`) takes import_source; `RemoveImportNamesOperation` (`:2538`) takes module_name/import_names. They mutate imports, not the complete caller graph. A whole-module symbol relocation is available through RelocateSymbolsToModuleOperation/RelocateSymbolsToNewModuleOperation (`module_move_codemod.py:922–931`), but neither replaces the member extraction or proves a changed write path. Resolve all bindings and the original export contract before choosing it. |

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

Add native binding cases in the planned `tests/test_declared_qt_fields.py`: a refused before_mutation callback leaves state/undo history unchanged; one accepted edit produces one state mutation and the original notification order; a member edit retains the Qt writer and narrowed annotation; outer optional None is accepted without calling reset; parent-state edits remain one atomic undo step. Exercise the real constructor/build/dispose owner rather than assigning manager internals. Extend the native widget tests with incomplete-ABC rejection and actual manager/control construction after the metaclass change.

On the declared Qt implementation stack, from the PyQtReactive checkout, run `PYTHONPATH=src python -m pytest -q -p no:xdist -o faulthandler_timeout=60 tests/test_declared_qt_fields.py tests/test_field_mutation_authorization.py tests/test_form_build_transaction.py tests/test_parameter_form_tree_index.py tests/test_annotation_choice_fields.py tests/test_inline_dataclass_chrome.py tests/test_scoped_table_widget.py tests/test_widgets.py`. The first file is a required implementation addition, not an existing passing test. This planning change does not create a Qt environment or run that command.

Add one shared-family test matrix for the conflicting values in §1.1, with native cases only for events/painting/validators. Exercise both literal and inherited forms with the same backend; a Textual-versus-Qt comparison alone would miss the missing domain distinction. Include a new scalar/choice field and show that only its declaration and irreducible native rendering need additions, with no consumer kind switches or rosters.

UI checks: pending invalid input, optional member feedback, save while focused, reset/undo, replaced/disposed form, model changes during staged mount, no signal echo and no orphan subscription/timer. The generic validation must execute before the state mutation and every authorized write must retain its original notification/transaction ordering.
