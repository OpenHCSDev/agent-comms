"""The owner's /prime: deal the rules again on demand, through the same gate a compaction uses.

"hey guys do /prime, you're all misaligned" (Tristan, 2026-10-30). A paste of the rules can be
skimmed; a deal cannot, because the gate refuses every other tool until each dealt file is read
and answered in a fresh audit. The variant picks which readers deal (full, no-papers,
no-indictment, light), so a mid-context prime need not fill the window.

Run with no target in a Claude session, it deals that session. With `--to`, it deals each named
thread whose deal Core holds (a native Pi thread); a Claude session's deal lives beside its own
transcript, so its thread family refuses and that session runs /prime itself.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from agent_comms.cli_bridges.claude_transcript import RunningClaude
from agent_comms.comms import client_wire
from agent_comms.pi_rule_gate import PiRedeal
from agent_comms_hooks.skill import DeckStart, FullPrime, Prime, SessionSource
from agent_comms_hooks.watch import WatchClaim


def session_start() -> DeckStart:
    """This Claude session as a deal: its id from the environment Claude exports to every Bash
    call, its transcript found by that id."""
    session = WatchClaim.environment_session()
    return DeckStart(session, "Prime", str(RunningClaude(session).source), SessionSource("prime"))


def prime_threads(prime: type[Prime], names: Sequence[str], root: Path | None) -> None:
    """Deal each named thread the variant's readers through its Core-held gate."""
    with client_wire(root) as comms:
        for name in names:
            thread = comms.registry.require(name)
            thread.execution.require_native()
            PiRedeal.of(comms, thread).deal_from(prime.commander, prime.fleet)
            print(f"{name}: dealt {prime.declared_name}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-comms-prime", description=__doc__)
    parser.add_argument("--variant", choices=Prime.names(), default=FullPrime.declared_name)
    parser.add_argument("--to", nargs="+", metavar="THREAD", default=(),
                        help="deal these native threads instead of this session")
    parser.add_argument("--root", type=Path, default=None)
    arguments = parser.parse_args(argv)
    prime = Prime.member_named(arguments.variant)
    if arguments.to:
        prime_threads(prime, arguments.to, arguments.root)
        return
    print("\n\n".join(session_start().primed(prime)))


if __name__ == "__main__":
    main()
