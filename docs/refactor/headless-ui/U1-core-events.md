# U1: Core events replace Textual messages

**Index:** [README.md](README.md). **First; starts now.** Patterns: IMPL-1, TIME-9, MEMB-5.

## What is wrong

The agent layer talks to the UI in Textual's own terms: 104 Textual `Message` classes, 20 of them in `acp/messages.py`, all subclasses of `AgentMessage(Message)`. `session_tracker.py` imports Textual only for its `Signal`, a publish-and-subscribe mechanism, and a `Widget` type annotation. As long as the protocol is Textual's, nothing below the UI can be headless.

## Target

```python
class CoreEvent(DeclaredFamily, affix="Event"):
    """Something the application reports to whatever renders it. Frontend-independent."""

@dataclass(frozen=True)
class ThinkingEvent(CoreEvent):
    session: SessionId
    text: str

class CoreEventStream:
    """The one publish-and-subscribe authority; replaces textual.signal.Signal in the core."""
    def publish(self, event: CoreEvent) -> None: ...
    def subscribe(self, listener: Callable[[CoreEvent], None]) -> Subscription: ...
```

- Each of the 20 `AgentMessage` subclasses becomes a `CoreEvent` member with the same fields; then the other 84 message classes, wherever they carry application facts rather than widget-internal ones.
- **The Textual frontend receives core events through one generic carrier,** a single Textual message holding a `CoreEvent`, posted into Textual's message pump; widgets dispatch on the event's class through `MroDispatch`. No per-event Textual message class mirrors a core event: that would be the same fact declared twice (MEMB-5), which is also how adapters start (TIME-9).
- Widget-internal Textual messages (a button press inside one widget) stay Textual's.
- **Every core event and command has a wire form from the start,** through `FieldCodec`, so a frontend in another process or another language can consume the same stream. That keeps DU4's TypeScript route open at no extra cost, and makes recorded sessions replayable for U6 and U7.

## Guards

No module under the core imports `textual`; no Textual message class whose fields restate a core event.

## Done when

`acp/messages.py` holds no Textual classes; `session_tracker` publishes on `CoreEventStream`; every application fact reaches widgets as a `CoreEvent`.
