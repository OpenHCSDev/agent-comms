# NRA catalog extraction refusal — exact lexical-scope cause

No product source was edited to avoid this refusal. No field was renamed, no class copied, no custom cache/facade introduced. The supported plan leaves the original RelationshipCatalogSource and its nominal provider pair OPEN. The original intended move remains in catalog-extraction-refused-plan.json.

## Real input

Core presentation.py:

* :18 imports `from agent_comms import relationships`.
* :603 declares the original WireRevision **class field** `relationships: Annotated[...]`.
* :605 uses `relationships.RelationshipStore.path_in(views.root)` in the original revision-source lambda.

The moved symbol is **RelationshipCatalogSource**, not RelationshipStore. The field declaration neither assigns the module's relationships variable nor refers to the moved catalog contract.

The bounded 49-file three-stage attempt reached extract_symbols_to_new_module and exited 1 with:

```
'.../src/agent_comms/presentation.py' rebinds 'relationships', which binds the source module 'agent_comms.relationships'
```

Retained: catalog-extraction-refused.log / .exit / -plan.json / -class-census.json.

## Determining native source branch

All paths below refer to the NRA package in `/home/ts/.cache/toad/test-env/ace79f6007097c3e/lib/python3.14/site-packages/nominal_refactor_advisor/`.

1. `module_node_index.py:18-35` explicitly documents its conservative rule that a class body is not represented as a separate scope.
2. `derive` at :57-119 walks all nodes, recording the imported relationships binding and the annotated class field's `ast.Name(..., Store())` as binding sites.
3. `_is_scope` at :229-230 accepts only function/lambda/comprehension nodes; `_scope_in` at :242-254 never recognizes the containing ClassDef. For the class field, `binding_scope` at :190-197 therefore reports None, meaning the module.
4. `module_scope_bindings` at :162-163 calls `bindings_in` (:174-188), which counts both sites in module scope: **2** instead of the real single module import binding.
5. `_SourceModuleObjectReads.of` in `refactors/module_move.py:1205-1209` rejects this count before filtering attribute reads by moved_names. The legitimate unmoved RelationshipStore read does not rescue it.

The repair belongs to NRA's lexical-scope owner and its consumers, retaining Python class/function/lambda lookup distinctions. Do not rename Core's valid field or bypass the refused move. This note identifies the causal branch; it is not a source patch for NRA.

## Minimal virtual input and exact operation

`catalog_binding_reproducer.py` uses the public frontend with CodemodSourceSnapshot.from_source_mapping. It creates **no physical fixture source files**. The complete inputs, operation and native preflight reports are retained in **catalog-class-field-reproducer.json**.

Virtual `pkg/relationships.py`:

```python
class RelationshipCatalogSource:
    catalog: object

class RelationshipStore:
    pass
```

Virtual `pkg/presentation.py`:

```python
from pkg import relationships

class WireRevision:
    relationships: int
    path = lambda self: relationships.RelationshipStore
```

There is also an empty virtual pkg/__init__.py. Operation:

```json
{
  "operation": "extract_symbols_to_new_module",
  "file_path": "<virtual>/pkg/relationships.py",
  "symbol_qualnames": ["RelationshipCatalogSource"],
  "destination_path": "<virtual>/pkg/catalog_source.py"
}
```

Recorded native results (catalog-binding-reproducer.log, exit **0**):

* **class_field:** module-binding count **2**, extraction **refused** with the same module-rebinding error. Both the ImportFrom and the class field's Name(Store) are reported as module scope.
* **no_field_control:** remove only `relationships: int`; count **1**, the **same operation simulates cleanly**, one stage.

This is an isolated native codemod simulation/control, not a pytest run, a Core import or a runtime product claim. Replay from this artifact directory:

```
/home/ts/.cache/toad/test-env/ace79f6007097c3e/bin/python catalog_binding_reproducer.py
```

After the lexical fix, rerun the original catalog-extraction-refused-plan.json against newly simulated current source; the minimal control alone is not proof of the real move or its imports.
