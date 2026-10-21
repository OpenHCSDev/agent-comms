"""Rule review for the commander: print every rule, nothing drawn, nothing stored.

The rules have one source each: the commander skill's bold-lead lines, every packaged rule file
in `rules/`, and paper 1's correct-maintenance results (the packaged correct-maintenance rule).

  agent-comms-commander rules review                 print every rule with its file:line
"""
import re
from pathlib import Path

from agent_comms import field_codec

from .config import CommanderConfig
from .order import Commander, CommanderVerb
from .rule_file import RuleFile

PAPER = f'{CommanderConfig.packaged_rules()}/correct-maintenance.md'


def principles() -> list[str]:
    """Paper 1's correct-maintenance results, the definitions every judgment is measured against."""
    text = RuleFile.read(Path(PAPER)).body
    return [l[2:].strip() for l in text.splitlines() if l.startswith('- ') and len(l) > 40]


def rules() -> list[tuple[str, str]]:
    """Every rule as (file:line, text)."""
    skill = CommanderConfig.packaged_skill()
    text = skill.read_text()
    found = [(f'{skill.name}:{text.count(chr(10), 0, m.start()) + 1}', (m.group(1) + m.group(2)).strip())
             for m in re.finditer(r'^\s*- \*\*(.+?)\*\*(.*)$', text, re.M)]
    for rule in RuleFile.packaged():
        found.extend((f'rules/{rule.name}.md:1', line) for line in rule.review_rules())
    return found


def review() -> None:
    for i, (where, r) in enumerate(rules(), 1):
        print(f'{i:3}. {where}  {r}')
    print('\nCorrect maintenance (paper 1; every judgment is measured against these):')
    for r in principles():
        print(f'  - {r}')


class RulesVerb(CommanderVerb):
    """Rules use the shared commander verb declaration."""


class ReviewVerb(RulesVerb):
    help = "print every standing rule"

    def run(self, ctx: Commander) -> None:
        review()


def main(a, ctx):
    RulesVerb.parse(a, __doc__).apply(ctx)
