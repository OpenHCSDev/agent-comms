"""Capture authored source references, without importing or executing product code."""

import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[3]
PATTERNS = {
    "event_and_lease_names": r"\bTurnState\b|\bTurnChangedUpdate\b|\bTurnTranscriptUpdate\b",
    "event_producers_dispatch": r"watchdog\.state|events\.TurnState|@handles\(|\.dispatch\(|_emit_event|def consume\(",
    "encoding_persistence": r"asdict\(|FieldCodec\.(encode|decode)|to_wire\(|from_wire\(|model_dump\(|session_update\(|record_terminal_failure|record_request_progress",
    "replay_owner_consumers": r"ReplayAssessments|ReplayFact|\.allows_retry|\.replay_safe|\.side_effects_possible",
    "removed_policy_proxies": r"tool_ever_started|output_started|compaction_started|event_phase|session\.(current|maximum|elapsed_ms)|TurnExposure",
    "retry_original": r"RetryAttemptEvent|AutoRetryStart|SummarizationRetryAttemptStart|SummarizationRetryScheduled|max_attempts|retry_reason_code",
    "phase_original": r"ShutdownPhase|def stalled|phase=|phase:.*TurnPhase",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def capture():
    files = sorted((ROOT / "src/agent_comms").rglob("*.py"))
    searches = {name: [] for name in PATTERNS}
    hashes = {}
    for path in files:
        relative = str(path.relative_to(ROOT))
        data = path.read_bytes()
        hashes[relative] = hashlib.sha256(data).hexdigest()
        for number, line in enumerate(data.decode().splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if re.search(pattern, line):
                    searches[name].append({"path": relative, "line": number, "source": line})
    changed = git("diff", "--name-only", "--", "src").decode().splitlines()
    return {
        "source_base": git("rev-parse", "HEAD").decode().strip(),
        "working_diff_sha256": hashlib.sha256(git("diff", "--", "src")).hexdigest(),
        "search_scope": "All authored src/agent_comms Python. Raw matches are source candidates, not resolved dynamic calls. No product imports or tests.",
        "file_count": len(files),
        "file_sha256": hashes,
        "changed_source": {name: hashes[name] for name in changed},
        "searches": searches,
        "counts": {name: len(rows) for name, rows in searches.items()},
    }


if __name__ == "__main__":
    Path(__file__).with_name("core-consumers.json").write_text(
        json.dumps(capture(), indent=2) + "\n"
    )
