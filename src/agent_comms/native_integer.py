"""The exact integer interval shared by Python, SQLite and the native JS writer."""

from typing import ClassVar


class NativeIntegerBounds:
    """A numeric capability; owners retain their own fields and wire contracts."""

    __slots__ = ()
    minimum: ClassVar[int] = 0
    maximum: ClassVar[int] = 2**53 - 1

    @classmethod
    def interval(cls) -> range:
        return range(cls.minimum, cls.maximum + 1)

    @classmethod
    def require_integer(cls, value: int, label: str) -> None:
        if value not in cls.interval():
            raise ValueError(f"{label} is outside its native range")

    @classmethod
    def sql_check(cls, column: str) -> str:
        return f"{column} BETWEEN {cls.minimum} AND {cls.maximum}"


class PositiveNativeIntegerBounds(NativeIntegerBounds):
    minimum = 1
