"""A packaged rule owns its metadata, reason, rendering and review membership."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, boundary_decoder
from agent_comms.team.package import TeamPackage


@dataclass(frozen=True)
class RuleFile(DeclaredFamily, affix="RuleFile"):
    family_discriminator = "type"
    name: str
    description: str
    drift: str
    wakeup: str
    why: str
    body: str
    path: Path
    """The packaged file this rule is read from: the deal points a session at it."""
    opening: ClassVar[re.Pattern[str]] = re.compile(
        r"\A((?:>[^\n]*\n)+)\n\*\*Why:\*\*[ \t]*(\S[^\n]*)$", re.M
    )
    """A rule opens with its wake-up, the greentext read before anything else, then its one
    Why line; the bullets follow."""

    @staticmethod
    @boundary_decoder
    def document(path: Path) -> tuple[dict[str, str], str]:
        """Read the packaged files' flat, single-line frontmatter once.

        Quoted scalars use JSON string spelling (a YAML subset); other scalars
        are plain text. Unsupported or malformed declarations fail here.
        """
        text = path.read_text(encoding="utf-8")
        header, separator, body = text.partition("\n---\n")
        if not header.startswith("---\n") or not separator:
            raise ValueError(f"{path.name}: missing frontmatter")
        metadata: dict[str, str] = {}
        for line in header.splitlines()[1:]:
            key, separator, value = line.partition(":")
            if not separator or key in metadata:
                raise ValueError(f"{path.name}: invalid frontmatter field {key!r}")
            value = value.strip()
            metadata[key] = (
                FieldCodec.decode(str, json.loads(value)) if value.startswith('"') else value
            )
        return metadata, body.strip()

    @classmethod
    @boundary_decoder
    def read(cls, path: Path) -> Self:
        metadata, body = cls.document(path)
        opening = cls.opening.match(body)
        if opening is None:
            raise ValueError(f"{path.name}: does not open with its wake-up greentext, then **Why:**")
        wakeup, why = opening.groups()
        return FieldCodec.decode(cls, {**metadata, "wakeup": wakeup.rstrip(), "why": why,
                                       "body": body[opening.end():].strip(), "path": str(path)})


    @classmethod
    def packaged(cls) -> tuple[Self, ...]:
        return tuple(cls.read(path) for path in TeamPackage.rules_files())

    @classmethod
    def order_line(cls) -> str:
        """The order cites the same rules its holder carries, without a role split."""
        names = ", ".join(rule.name for rule in cls.packaged())
        return f"Standing rules for this work: {names} in {TeamPackage.rules()}."

    @property
    def summary(self) -> str:
        return f"{self.name}: {self.description}\nWhy: {self.why}"

    def render(self) -> str:
        return f"{self.wakeup}\n\n{self.summary}\n\n{self.body}".rstrip()

    @property
    def reminder(self) -> str:
        """The inline slice that keeps this rule awake between reads: its wake-up, then its tell."""
        return f"{self.wakeup}\n\n{self.drift}"


class FeedbackRuleFile(RuleFile):
    """A rule that supplies feedback in the teammate's instructions."""


class HardRuleFile(FeedbackRuleFile, declared_name="hard-rule"):
    """A feedback rule that is never waived."""


class ReferenceRuleFile(RuleFile):
    """Supporting instructions read alongside the feedback rules."""
