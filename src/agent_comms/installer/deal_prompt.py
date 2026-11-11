"""The system prompt a Core-launched session carries: the host configuration, then the whole
deal in the order the gate deals it, papers first.

A system prompt is read before the first token and survives every compaction, with the
authority of the system prompt rather than of a file a tool returned. So a session launched with
it has read its deal already, and the gate asks it for the witness only: the audit and "did you
mean it?" before its first tool call. Beside the prompt, its manifest records each dealt file as
a read of the exact content the prompt carries (rule_deal.SystemPromptDelivery reads it back).
Every launcher that can put a file in its runtime's system prompt passes this one: native Pi
through --append-system-prompt, Claude Code through claude_launch's
--append-system-prompt-file.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_comms.installer.native_package import OWNER_INSTRUCTIONS, SYSTEM_PROMPT
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.team.prompt_reads import DealtRead, DealtSource, PreparedPrompt, PromptReads
from agent_comms_hooks.skill import DeckReader


@dataclass(frozen=True)
class DealPrompt:
    """The prompt text and the reads it carries, rendered together from the same bytes, so the
    manifest can never claim content the prompt does not hold."""

    text: str
    reads: PromptReads

    preamble: str = (
        "# Your deal\n\n"
        "Below is everything the rule gate deals you, in its order: the two papers every rule is"
        " a consequence of, then the rules, the voice, the recorded failures and the pattern catalog."
        " You have read them by reading this prompt. Before your first tool call you answer every"
        " rule in your audit, and then Tristan asks whether you meant it: the gate refuses every"
        " other tool until both are done, and its refusal names the audit file and its template."
    )

    @classmethod
    def render(cls) -> DealPrompt:
        """One read of each dealt file supplies both its section and its digest."""
        sections = [OWNER_INSTRUCTIONS.read_text(encoding="utf-8").strip(), cls.preamble]
        reads = []
        for path in DeckReader.sources():
            raw = path.read_bytes()
            source = DealtSource.of(path)
            reads.append(DealtRead(source, hashlib.sha256(raw).hexdigest()))
            sections.append(f"## {source.package}/{source.relative}\n\n{raw.decode('utf-8').strip()}")
        return cls("\n\n".join(sections) + "\n", PromptReads(tuple(reads)))

    @classmethod
    def prepare(cls, path: Path = SYSTEM_PROMPT) -> Path:
        """Publish this deal under its own content name, its manifest beside it, then point
        `path` at it. A launcher resolves `path`, so every session keeps the deal it launched
        with: a later install publishes a new name instead of rewriting the old one, and a file
        that changed since stops matching that session's manifest and is dealt to it again."""
        prompt = cls.render()
        digest = hashlib.sha256(prompt.text.encode("utf-8")).hexdigest()[:16]
        launched = path.with_name(f"{path.stem}-{digest}{path.suffix}")
        cls.publish(PreparedPrompt(launched).manifest, json.dumps(FieldCodec.encode(prompt.reads)))
        cls.publish(launched, prompt.text)
        link = path.with_name(f".{path.name}.link")
        link.unlink(missing_ok=True)
        link.symlink_to(launched.name)
        os.replace(link, path)
        return launched

    @staticmethod
    def publish(path: Path, text: str) -> None:
        descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.chmod(staged, 0o644)
            os.replace(staged, path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise


class CloudDealPrompt(DealPrompt):
    """The same deal for a session with no launcher and no Core: a cloud session, whose setup
    (rule_gate.py install) puts it in the system prompt as an output style. Only what it is told
    differs, because Core's audit gate is not there to enforce what the launched prompt promises."""

    preamble = (
        "# Your deal\n\n"
        "Below is everything the rule gate deals you, in its order: the two papers every rule is"
        " a consequence of, then the rules, the voice, the recorded failures and the pattern catalog."
        " You have read them by reading this prompt, and it survives every compaction, so do not"
        " read them again with a tool unless the gate names a file that changed. Engage with them"
        " before you act. Many steps name the bus host's tools; rules/spine.md says how"
        " to keep each step's reason with what this session has."
    )
