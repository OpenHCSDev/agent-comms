"""Provider error records shared by native payloads and turn phases."""
from __future__ import annotations

from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .pi_payload import PiPayload


@dataclass(frozen=True)
class ProviderDiagnosticError(PiPayload, DeclaredFamily, affix="ProviderDiagnosticError"):
    message: str
    name: str | None = None

    @classmethod
    def wire_member(cls, value: dict[str, object]) -> type[ProviderDiagnosticError]:
        return CodedProviderDiagnosticError if "code" in value else CodelessProviderDiagnosticError

    @property
    def description(self) -> str:
        return self.name or "Provider transport error"


@dataclass(frozen=True)
class CodelessProviderDiagnosticError(ProviderDiagnosticError):
    """Pi reported a provider error without a code."""


@dataclass(frozen=True, kw_only=True)
class CodedProviderDiagnosticError(ProviderDiagnosticError):
    code: int | str

    @property
    def description(self) -> str:
        return f"{super().description} (code {self.code})"
