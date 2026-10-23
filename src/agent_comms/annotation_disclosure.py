"""Disclosure capabilities shared by native messages and context segments."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .turn_context import ContextSpan


class WithheldAnnotations:
    """Source text cannot be disclosed to an annotation provider."""

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import WithheldDisclosure

        return WithheldDisclosure()


class PublicInstructionAnnotations:
    """Instruction text uses the existing credential-filtering disclosure policy."""

    def disclosure_for(self, span: ContextSpan):
        from .working_memory_disclosure import PublicInstructionDisclosure

        return PublicInstructionDisclosure()
