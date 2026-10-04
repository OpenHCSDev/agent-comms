# F3: The context explorer's lifecycle

**Index:** [README.md](README.md). **Repository:** Toad. **Fold into #417,** which already changes both files. **Pattern:** IDEN-3.

## What is wrong

#309 rightly split inspection logic into `core/context_inspection.py`, the first module of a headless core, from the widget `widgets/context_explorer.py`. The widget keeps its own lifecycle as optional fields: `self._native is None`, `self._inspection is None`, `node is not None and node.data is not None`, plus previous-against-current manifest comparisons field by field (`same_source = previous is not None and ...`, `changed_manifests = previous is None or ...`).

## Target

The explorer's lifecycle as a state family: detached, reading an owner, holding an inspection. Each state carries only its own data and answers what the widget currently asks through `None` checks. The manifest comparison is a method on the manifest (`changed_since(previous)`), and the logic stays in `core/`.

## Done when

The explorer holds one state object instead of optional fields, and no previous-against-current field comparison remains in the widget.
