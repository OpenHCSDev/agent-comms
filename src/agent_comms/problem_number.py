"""The canonical spelling and numeric value of a global problem identity."""
from dataclasses import dataclass
from typing import ClassVar, Self


@dataclass(frozen=True, slots=True)
class ProblemNumber:
    number: int
    prefix: ClassVar[str] = 'P'

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError('A problem number is positive')

    def __str__(self) -> str:
        return f'{self.prefix}{self.number}'

    @classmethod
    def parse(cls, text: str) -> Self:
        number = cls(int(text.removeprefix(cls.prefix)))
        if str(number) != text:
            raise ValueError(f'Not a canonical problem number: {text!r}')
        return number

    @classmethod
    def accepts(cls, text: str) -> bool:
        try:
            cls.parse(text)
        except ValueError:
            return False
        return True

    @classmethod
    def sql_number(cls, column: str) -> str:
        """The numeric projection of an already validated stored identity."""
        return f'CAST(substr({column}, {len(cls.prefix) + 1}) AS INTEGER)'
