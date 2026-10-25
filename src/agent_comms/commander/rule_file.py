"""A packaged rule owns its metadata, reason, rendering, review membership and audience."""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from agent_comms.commander.package import CommanderPackage
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, boundary_decoder


class RuleAudience(DeclaredFamily, affix="Audience"):
    """Who a packaged rule governs. Every unit carries every rule, so it can hold the fleet
    and the commander to them; each audience owns how its rules are introduced."""

    heading: ClassVar[str]

    @classmethod
    @abstractmethod
    def order_line(cls, names: str) -> str:
        """The order footer line naming this audience's rules."""

    @classmethod
    def grouped(cls, rules: tuple[RuleFile, ...]) -> tuple[tuple[type[RuleAudience], tuple[RuleFile, ...]], ...]:
        """The rules under each audience, audiences in declaration order."""
        audiences = sorted({rule.audience for rule in rules}, key=lambda audience: audience.declared_position())
        return tuple((audience, tuple(rule for rule in rules if rule.audience is audience))
                     for audience in audiences)


class FleetAudience(RuleAudience):
    """How code and reports are judged: every unit follows it, and so does the commander."""

    heading = "# Rules every unit follows"

    @classmethod
    def order_line(cls, names: str) -> str:
        return f"Standing rules for this order: follow the fleet rules {names} in {CommanderPackage.rules()}."


class CommanderAudience(RuleAudience):
    """The commander's own process (roasts, strikes, timers, coordination): units carry it to
    judge the commander by it, never to act as the commander."""

    heading = ("# Rules the commander follows\n\nThey apply to the commander too: roast it with "
               "file:line when it breaks one. They give no unit a commander action.")

    @classmethod
    def order_line(cls, names: str) -> str:
        return (f"The commander's rules {names} in {CommanderPackage.rules()} apply to the "
                "commander too; roast it with file:line when it breaks one.")


@dataclass(frozen=True)
class RuleFile(DeclaredFamily, affix="RuleFile"):
    family_discriminator = "type"
    name: str
    description: str
    drift: str
    audience: type[RuleAudience]
    why: str
    body: str
    reason_line: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:\*\*Why:\*\*|Why:)[ \t]*(\S[^\n]*)$", re.M
    )

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
        reason = cls.reason_line.search(body)
        if reason is None:
            raise ValueError(f"{path.name}: missing Why")
        return FieldCodec.decode(
            cls, {**metadata, "why": reason.group(1), "body": cls.reason_line.sub("", body).strip()}
        )

    @classmethod
    def packaged(cls) -> tuple[Self, ...]:
        return tuple(
            sorted(
                (cls.read(path) for path in CommanderPackage.rules_files()),
                key=lambda rule: (cls.kinds().index(type(rule)), rule.name),
            )
        )

    @classmethod
    def kinds(cls) -> tuple[type[RuleFile], ...]:
        """Every rule kind in reading order, derived from the hierarchy: a kind that refines
        another comes before it, and sibling kinds keep their declaration order."""
        return (*(kind for member in cls.__subclasses__() for kind in member.kinds()), cls)

    @property
    def summary(self) -> str:
        return f"{self.name}: {self.description}\nWhy: {self.why}"

    def render(self) -> str:
        return f"{self.summary}\n\n{self.body}".rstrip()

    @classmethod
    def drift_tells(cls) -> tuple[str, ...]:
        """The reminder hook's one-line tells, one declared by each packaged rule."""
        return tuple(rule.drift for rule in cls.packaged())


class FeedbackRuleFile(RuleFile):
    """A rule that supplies feedback in the commander's instructions."""


class HardRuleFile(FeedbackRuleFile, declared_name="hard-rule"):
    """A feedback rule that is never waived; refining FeedbackRuleFile puts it first."""


class ReferenceRuleFile(RuleFile):
    """Supporting instructions read alongside the feedback rules."""
