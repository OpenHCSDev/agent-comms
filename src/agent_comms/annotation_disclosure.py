"""Disclosure policies, and the capabilities that give a message or segment its policy."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .turn_context import ContextSpan


class DisclosureWithheld(ValueError):  # noqa: N818 - nominal refusal state
    """The original segment's policy does not permit this text to leave."""


class DisclosurePolicy(DeclaredFamily, affix="Disclosure"):
    @abstractmethod
    def require_text(self, text: str) -> str: ...


@dataclass(frozen=True)
class WithheldDisclosure(DisclosurePolicy):
    def require_text(self, text: str) -> str:
        raise DisclosureWithheld("This original segment does not permit third-party disclosure")


@dataclass(frozen=True)
class PublicInstructionDisclosure(DisclosurePolicy):
    # Credential-shaped values are withheld as a whole. Replacing them would
    # make the classifier label prose which is not the addressed original span.
    credential = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]+"
        r"|\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*\S+"
        r"|\bBearer\s+\S+", re.IGNORECASE)

    def require_text(self, text: str) -> str:
        if self.credential.search(text):
            raise DisclosureWithheld("Credential-shaped original text cannot be disclosed")
        return text


class WithheldAnnotations:
    """Source text cannot be disclosed to an annotation provider."""

    def disclosure_for(self, span: ContextSpan) -> DisclosurePolicy:
        return WithheldDisclosure()


class PublicInstructionAnnotations:
    """Instruction text uses the existing credential-filtering disclosure policy."""

    def disclosure_for(self, span: ContextSpan) -> DisclosurePolicy:
        return PublicInstructionDisclosure()
