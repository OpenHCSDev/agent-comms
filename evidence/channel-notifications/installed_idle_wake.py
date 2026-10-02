"""Measure actual idle owners; optionally submit one fresh channel wake probe."""
import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from agent_comms.comms import wire

NAMES = ("agent-comms-ux", "pr95-selected-pi-summary-owner")
HERE = Path(__file__).resolve().parent


def measure(w):
    before = {n: w.registry.require(n) for n in NAMES}
    assert all(o.active_turn is None for o in before.values()), "Owner is working"

    def ticks(pid):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])

    initial = {n: ticks(o.pid) for n, o in before.items()}
    start = time.monotonic()
    time.sleep(5)
    final = {n: ticks(o.pid) for n, o in before.items()}
    elapsed = time.monotonic() - start
    after = {n: w.registry.require(n) for n in NAMES}
    assert all(after[n].pid == o.pid and after[n].active_turn is None for n, o in before.items())
    return {"seconds": elapsed, "owners": {
        n: {"pid": o.pid, "active_turn": None,
            "percent_of_one_core": round(100 * (final[n] - initial[n]) / os.sysconf("SC_CLK_TCK") / elapsed, 2)}
        for n, o in before.items()
    }}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wake", action="store_true")
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    w = wire()
    report = {"verified": False, "messages_sent": 0, "replayed_inputs": 0}
    output = HERE / f"installed-idle-{args.label}.json"
    try:
        report["idle"] = measure(w)
        print(json.dumps(report["idle"]), flush=True)
        if args.wake:
            marker = f"IDLE_WAKE_OK_{uuid4().hex[:8]}"
            start = time.monotonic()
            message = w.messaging.send_user_message(
                "#comms", f"Live idle-CPU fix verification. @agent-comms-ux: reply {marker} in this channel. "
                "No tools, file edits, or old-task resumption. PR95 owner: this concerns only the Comms UX owner; ignore it.",
                worktree="/home/ts/.agent-comms",
            )
            report.update(messages_sent=1, sequence=message.seq, message_id=message.message_id, marker=marker, observations=[])
            previous = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                rows = w.views.message_notifications([message])[(message.seq, message.message_id)]
                states = {r.recipient: r.state for r in rows if r.recipient in NAMES}
                if states != previous:
                    observation = {"seconds": round(time.monotonic() - start, 3), "states": states}
                    report["observations"].append(observation)
                    print(json.dumps(observation), flush=True)
                    output.write_text(json.dumps(report, indent=2) + "\n")
                    previous = states
                # The explicit @mention selects UX. Other channel members are
                # unmentioned observers and need not get a model assignment.
                if states.get(NAMES[0]) == "Responded":
                    break
                time.sleep(.2)
            else:
                raise AssertionError(f"Fresh channel wake did not finish: {previous}")
            with w.bus.log.full_history_snapshot() as (_, history):
                replies = [m for m in history if m.seq > message.seq and m.sender == NAMES[0] and marker in m.body]
            assert len(replies) == 1, "Expected one actual channel reply to the fresh probe"
            report["reply"] = {"sequence": replies[0].seq, "message_id": replies[0].message_id, "body": replies[0].body}
            # Measure again after actual work has finished, not during an inference.
            time.sleep(2)
            report["idle_after_reply"] = measure(w)
        report["verified"] = True
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
