# U4: The frontend family

**Index:** [README.md](README.md). **After U3.** Patterns: IMPL-3, MEMB-2.

## Target

```python
class Frontend(DeclaredFamily, affix="Frontend"):
    """Renders the core's state natively and sends intents back."""
    views: ClassVar[ViewRegistry]          # view-state class -> this frontend's view

class Capability: ...                      # declared per frontend, never probed
class InlineImages(Capability): ...
class EmbeddedTerminal(Capability): ...

class TextualFrontend(Frontend, InlineImages, EmbeddedTerminal): ...
class OpenTUIFrontend(Frontend, InlineImages, EmbeddedTerminal): ...
```

- **Each frontend has a registry of views keyed by view-state class,** resolved through the class's ancestry. A new state family fails loudly in any frontend that has not declared a view for it, instead of rendering nothing.
- **Capabilities are declared, never probed:** the core asks `frontend.supports(InlineImages)`, a capability check, and never inspects widget types.
- `TextualFrontend` is today's widgets, registered as views. No behaviour changes in this surface.

## Done when

Every view Toad renders is registered on `TextualFrontend`; nothing in the core refers to a widget.
