# Native forms: admitted field paths

Owner: core-route-counts. This is a source checkpoint, not installed acceptance.

## Before and after

Baseline: Core `424138c04`, Toad `907840a70`, ObjectState `fce5b7f`, FormBridge `119a208`.

- ObjectState `ParameterStructure.direct_paths` owns admitted addresses and order. New `path`, `ancestors` and `leaf_paths` operations return addresses from that topology, not a second declaration analysis or cache.
- FormBridge `ParameterFormService.parameters_for_state` now yields each original `DottedFieldPath` with its existing parameter-kind information. Its sole production consumer, Toad `FieldView.of`, passes that address to `DeclaredField`; the runtime `getattr(parent, parameter.name)` is deleted.
- `DeclaredField.path` is a `DottedFieldPath`, resolved through the active structure when bound. Declarations, values and edits remain reads/writes of the existing ObjectState. Static `FieldPath.bind` checks the root type and resolves through the same structure. The static declaration DSL remains; runtime `steps`, `leaves`, `fields_below` and `declared_field` helpers are deleted.
- SavedSettings no longer keeps `named` or reconstructs topology. Change handling is bounded by changed paths; file presence and output selection use ObjectState's direct children. `in_file`/`user_set` still distinguish explicit keys from omitted defaults, including empty groups and explicit values equal to defaults. An explicit `set` to the current default also records the key after successful validation.
- PreferenceChanged carries the existing bound field. Sidebar, store, conversation and recovery consumers match their static declaration against it rather than rebuilding a runtime static path.
- Wire names remain FieldCodec's fact. Its existing cached `_fields` implementation is renamed `record_fields`, with all codec consumers migrated and no compatibility alias. SavedSettings reads those keys instead of interpreting field metadata independently. This API is required by the paired Toad branch; old installed Core is insufficient.

## Source evidence and checks

NRA's existing syntax/class-family index covered 744 production/dependency modules before and after: 4,363 original classes, 4,352 nominally projected, 11 OPEN; no parse errors. This retrieval covers declarations, imports, writes, checks and consumers in Core, Toad, FormBridge, ObjectState and python-introspect. Dynamic aliases, C3 execution and invocation effects are not proven by that index. No new class or registry was added for this correction; adding a settings field does not require a second topology enrollment.

Source and detailed site evidence: `/home/ts/.cache/agent-scratch/core-native-formbridge/path-{before,after}-*`; owner/consumer search: `runtime-path-consumers.txt`. These are owned disposable analysis files; retain concise source/results here after acceptance.

Changed Python source parses and whitespace checks pass. Search finds no runtime `fields_below`, `leaves`, `steps`, `self.named` or `getattr(parent, parameter.name)` in the converted Toad family. Existing settings-screen assertions were migrated to admitted paths; no Toad automated tests were added or run.

Core codec/ownership checks: **34 passed in 2.80s** (`tests/test_field_codec.py`, `tests/guards/test_field_codec_ownership.py`). This does not verify form construction, persistence, native geometry/paint, focus, theme changes or the installed application.

## Remaining delivery

Resolve exact paired Textual #134/#135 and Toad shared-modal changes with the ObjectState/FormBridge/Core source pins and matching lock. Large drafts still require Claude review. No native-form application acceptance or global installation is claimed.

The earlier candidate command has no returned outcome. Read-only inspection found its resolution log, no candidate install log or `runtime/pyvenv.cfg`, and no matching uv process; these observations do not establish absence of side effects. That command was not retried and the persistent goal remains BLOCKED. New source correction work is distinct.
