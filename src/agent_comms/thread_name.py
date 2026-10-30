"""The spelling rule shared by thread declarations and addressed targets."""

from typing import ClassVar


class ThreadName:
    characters: ClassVar[frozenset[str]] = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    @classmethod
    def require(cls, name: str) -> None:
        if not name or not set(name) <= cls.characters:
            raise ValueError(
                f"Thread name {name!r} must be alphanumeric with hyphens/underscores."
            )
