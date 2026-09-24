"""Sender for one private N/K cohort.

Start each recipient with ``python -m agent_comms.cohort_foreground`` and wait
for its ready receipt before sending. This command publishes one frozen initial
N/K envelope; the foreground owners independently accept N and claim K. It
never starts a model, waits for replies, ACKs an inbox, or retries a send.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .coordination_store import IdentityConflict, PublicationActivationBlocked
from .declarations import _store_lock
from .native_pi import _private_session_dir
from .operations import Comms


def publish_one(
    root: Path,
    *,
    wire_root_id: str,
    sender: str,
    target: str,
    body: str,
    opt_in: bool = True,
) -> tuple[int, str]:
    """Publish exactly one private initial on an initialized root."""
    root = Path(root).absolute()
    if not opt_in or root == Path("/var/tmp") or not root.is_relative_to("/var/tmp"):
        raise PublicationActivationBlocked("cohort sender requires a private /var/tmp root")
    _private_session_dir(root)
    comms = Comms(root, private_initial_writes=True)
    with _store_lock(comms.bus._path):
        marker = comms.bus._private_marker_unlocked()
    if marker["wire_root_id"] != wire_root_id:
        raise IdentityConflict("private initial wire root changed")
    message = comms.send_initial_cohort(sender, target, body)
    return message.seq, message.message_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wire-root-id", required=True)
    parser.add_argument("--from", dest="sender", required=True)
    parser.add_argument("--to", dest="target", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--opt-in", action="store_true", default=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        seq, message_id = publish_one(
            args.root,
            wire_root_id=args.wire_root_id,
            sender=args.sender,
            target=args.target,
            body=args.body,
            opt_in=args.opt_in,
        )
    except Exception as error:
        # No message body or provider diagnostic in public receipts. On an
        # uncertain append, do not issue another send: inspect the private bus.
        print(json.dumps({"error_type": type(error).__name__, "root_retained": True}), flush=True)
        return 1
    print(json.dumps({"wire_seq": seq, "message_id": message_id}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
