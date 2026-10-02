"""Count original ACP publications; never send, reopen or alter an attempt.

Evidence tooling only: JSON fields here are the original wire observations,
not a new product decision, state owner, proof or presentation projection.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("log", type=Path)
parser.add_argument("--session", required=True)
parser.add_argument("--operation", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
raw = args.log.read_bytes()
progress = []
turns = []
events = Counter()
rpc_results = []
for number, encoded in enumerate(raw.splitlines(), 1):
    if not encoded.startswith(b"[agent] "):
        continue
    record = json.loads(encoded[len(b"[agent] "):])
    if "result" in record:
        result = record["result"]
        if isinstance(result, dict) and "stopReason" in result:
            rpc_results.append({"line": number, "request_id": record["id"],
                                "stop_reason": result["stopReason"]})
    params = record.get("params", {})
    if params.get("sessionId") != args.session:
        continue
    updates = params.get("update", {}).get("_meta", {}).get("agentComms", {}).get("updates", [])
    for update in updates:
        kind = update["kind"]
        if kind == "turn_changed":
            active = update["state"]["active"]
            if active is not None:
                phase = active["phase"]
                if phase.get("operation_id") == args.operation:
                    turns.append({"line": number, "turn_id": active["id"],
                                  "phase": phase["kind"], "source": phase.get("source")})
            continue
        if kind != "compaction_changed":
            continue
        event = update["event"]
        if event.get("operation_id") != args.operation:
            continue
        events[event["kind"]] += 1
        source = event.get("source")
        if source is not None:
            progress.append({"line": number, "source": source,
                             "text_chars": len(event.get("text", ""))})


def aggregate(source):
    if source is None:
        return None
    return (source["sourceBytesDone"], source["sourceBytesTotal"], source["startedAtMs"])


phases = Counter(row["source"]["summaryPhase"] for row in progress)
leaf_switches = sum(a["source"]["summaryPhase"] != b["source"]["summaryPhase"]
                    for a, b in zip(progress, progress[1:]))
work_changes = sum(aggregate(a["source"]) != aggregate(b["source"])
                   for a, b in zip(progress, progress[1:]))
repeated_turn_work = sum(a["turn_id"] == b["turn_id"] and
                        a["phase"] == b["phase"] and
                        aggregate(a["source"]) == aggregate(b["source"])
                        for a, b in zip(turns, turns[1:]))
report = {
    "log": str(args.log), "log_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
    "log_snapshot_bytes": len(raw), "session": args.session, "operation": args.operation,
    "event_counts": dict(events), "source_observations": len(progress),
    "text_records": sum(bool(row["text_chars"]) for row in progress),
    "text_chars": sum(row["text_chars"] for row in progress),
    "leaf_observation_counts": dict(phases), "leaf_switches": leaf_switches,
    "aggregate_work_changes": work_changes, "whole_turn_publications": len(turns),
    "repeated_whole_turn_work_publications": repeated_turn_work,
    "first_source_observation": progress[0] if progress else None,
    "last_source_observation": progress[-1] if progress else None,
    "whole_turn_observations": turns, "prompt_terminal_results_in_log": rpc_results,
    "provider_terminal_clock": "not exposed by ACP source progress",
    "scope": "Original source-consumption clocks, not network arrival or UI paint clocks; terminal SQL/native/UI proof joins separately.",
    "read_only": True, "sent_input": False,
}
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({key: value for key, value in report.items()
                  if key not in {"whole_turn_observations", "first_source_observation"}}, indent=2))
