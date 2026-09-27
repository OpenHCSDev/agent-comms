"""Class-owned asynchronous handlers, composed along the value's C3 MRO."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

Handler = TypeVar("Handler", bound=Callable[..., Awaitable[Any]])


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

    async def dispatch(self, value: Any) -> Any:
        methods: dict[str, Any] = {}
        for owner in type(self).__mro__:
            for name, method in vars(owner).items():
                methods.setdefault(name, method)
        for capability in type(value).__mro__:
            for name, method in methods.items():
                if capability in getattr(method, "__handled_classes__", ()):
                    replacement = await getattr(self, name)(value)
                    if replacement is not None:
                        if type(replacement) is not type(value):
                            raise TypeError("A dispatch replacement must preserve event identity")
                        value = replacement
        return value
