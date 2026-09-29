"""Mechanisms permit implementation subclasses only in their owning module."""


class Sealed:
    __slots__ = ()

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        mechanisms = tuple(
            base for base in cls.__bases__
            if base is not Sealed and issubclass(base, Sealed)
        )
        for mechanism in mechanisms:
            if cls.__module__ != mechanism._sealed_home:
                raise TypeError(
                    f"{cls.__qualname__} adapts the sealed mechanism "
                    f"{mechanism.__qualname__}; its implementation belongs in "
                    f"{mechanism._sealed_home}"
                )
        if not mechanisms:
            cls._sealed_home = cls.__module__
