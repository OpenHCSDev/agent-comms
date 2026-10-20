"""A packaged rule owns its metadata, reason, rendering and review membership."""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from ..declared_family import DeclaredFamily
from ..field_codec import FieldCodec, boundary_decoder
from .config import CommanderConfig


@dataclass(frozen=True)
class RuleFile(DeclaredFamily, affix="RuleFile"):
    family_discriminator = "type"
    name: str
    description: str
    why: str
    body: str
    priority: ClassVar[int]
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
                (cls.read(path) for path in CommanderConfig.packaged_rules_files()),
                key=lambda rule: (rule.priority, rule.name),
            )
        )

    @property
    def summary(self) -> str:
        return f"{self.name}: {self.description}\nWhy: {self.why}"

    def render(self) -> str:
        return f"{self.summary}\n\n{self.body}".rstrip()

    @abstractmethod
    def review_rules(self) -> tuple[str, ...]: ...


class FeedbackRuleFile(RuleFile):
    priority = 1

    def review_rules(self) -> tuple[str, ...]:
        return (self.summary,)


class HardRuleFile(FeedbackRuleFile, declared_name="hard-rule"):
    priority = 0


class ReferenceRuleFile(RuleFile):
    priority = 2

    def review_rules(self) -> tuple[str, ...]:
        return ()
