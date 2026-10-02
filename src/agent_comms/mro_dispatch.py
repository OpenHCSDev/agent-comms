"""Class-owned handlers, composed along the value's C3 MRO."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

Handler = TypeVar("Handler", bound=Callable[..., Any])


def handles(*classes: type) -> Callable[[Handler], Handler]:
    """Declare the nominal cases handled by a consumer method."""

    def decorate(handler: Handler) -> Handler:
        handler.__handled_classes__ = classes  # type: ignore[attr-defined]
        return handler

    return decorate


class MroDispatch:
    """Invoke specific handlers before base handlers; consumer overrides win.

    The method declarations are the authority. No global registry, registration
    order, or event-name roster participates in dispatch. A diamond ancestor is
    visited once by Python's MRO. A returned replacement flows to later handlers.
    """

    def handlers_for(self, value: Any):
        methods: dict[str, Any] = {}
        for owner in type(self).__mro__:
            for name, method in vars(owner).items():
                methods.setdefault(name, method)
        for capability in type(value).__mro__:
            for name, method in methods.items():
                if capability in getattr(method, "__handled_classes__", ()):
                    yield getattr(self, name)

    async def dispatch(self, value: Any) -> Any:
        handlers = tuple(self.handlers_for(value))
        if not handlers:
            return value
        return await self.consume_handlers(value, handlers)

    async def consume_handlers(self, value: Any, handlers: Iterable[Handler]) -> Any:
        """Consume the selected declarations inside the consumer's resource lifetime."""
        for handler in handlers:
            value = self.replace_value(value, await handler(value))
        return value

    def dispatch_sync(self, value: Any) -> Any:
        """Consume saved presentation facts without introducing an event loop."""
        return self.consume_handlers_sync(value, self.handlers_for(value))

    def consume_handlers_sync(self, value: Any, handlers: Iterable[Handler]) -> Any:
        for handler in handlers:
            value = self.replace_value(value, handler(value))
        return value

    @staticmethod
    def replace_value(value: Any, replacement: Any) -> Any:
        if replacement is None:
            return value
        if type(replacement) is not type(value):
            raise TypeError("A dispatch replacement must preserve event identity")
        return replacement
