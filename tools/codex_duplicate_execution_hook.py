"""Reject declared duplicate launches using the original lifecycle records.

Coverage is deliberately narrow: one direct shell command uniquely matching
stage_argv, proof_argv or controller_argv. Reused commands cannot identify their
purpose and are outside coverage. Shell programs, Python wrappers and changed
argv are not interpreted. This hook never reserves or writes an attempt.

This is not an atomic reservation or a security boundary against unrestricted
shell/root access. Existing sessions may need to reload their hook configuration.
Only the declared handle fields below support running-process checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys


@dataclass(frozen=True)
class Operation:
    name: str
    argv_key: str
    attempts_key: str
    authorization_key: str
    handle_key: str

    def refusal(self, record: dict, path: Path) -> str | None:
        attempts = record.get(self.attempts_key)
        if type(attempts) is not int or attempts < 0:
            return f"Cannot establish {self.name} attempt state from {path}."
        if attempts:
            return (
                f"{self.name} already consumed ({attempts} attempt(s)): {path}. "
                "Follow the original handle or completed result; do not relaunch."
            )
        handle = record.get(self.handle_key)
        if handle is not None:
            raw = Path(handle["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != handle["sha256"]:
                return f"Cannot establish original {self.name} handle integrity: {path}."
            identities = json.loads(raw)
            for role in ("controller", "child"):
                identity = identities.get(role)
                if identity is not None and process_exists(identity):
                    return (
                        f"Original {self.name} {role} is still running: {path}. "
                        "Follow that handle; do not relaunch."
                    )
        if record.get(self.authorization_key) is not True:
            return f"{self.name} is not released in its existing lifecycle: {path}."
        return None


OPERATIONS = (
    Operation("stage", "stage_argv", "stage_attempts_consumed", "stage_authorized", "actual_stage_handle"),
    Operation("proof", "proof_argv", "proof_attempts_consumed", "proof_execution_authorized", "actual_proof_handle"),
    Operation("App", "controller_argv", "control_attempts_consumed", "execution_authorized", "actual_control_handle"),
)


def process_exists(identity: dict) -> bool:
    pid, birth = identity["pid"], identity["start_time"]
    if type(pid) is not int or type(birth) is not int or pid <= 0:
        raise ValueError("Invalid original process identity")
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    # comm is parenthesized and may itself contain spaces or parentheses.
    fields = stat[stat.rfind(")") + 2 :].split()
    return int(fields[19]) == birth


def direct_argv(command: str) -> tuple[str, ...] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
    if not tokens or any(token in {";", "&&", "||", "|", "&", "(", ")", "<", ">", "<<", ">>"} for token in tokens):
        return None
    # A multi-command shell program is outside this guard's declared scope.
    if "\n" in command or "$" in command or "`" in command:
        return None
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] == "exec":
        tokens.pop(0)
    return tuple(tokens) or None


def refusal(event: dict, lifecycle_dir: Path) -> str | None:
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash":
        return None
    command = event.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        return None
    argv = direct_argv(command)
    if argv is None:
        return None
    matches = []
    for path in sorted(lifecycle_dir.glob("*lifecycle.json")):
        record = json.loads(path.read_bytes())
        for operation in OPERATIONS:
            declared = record.get(operation.argv_key)
            if isinstance(declared, list) and tuple(declared) == argv:
                matches.append((operation, record, path))
    # Equal argv does not erase distinct purpose identity. A bare shell command
    # cannot choose between those owners, so do not invent that decision here.
    if len(matches) != 1:
        return None
    operation, record, path = matches[0]
    return operation.refusal(record, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycle-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.lifecycle_dir.is_dir():
            raise ValueError("Original lifecycle directory is unavailable")
        reason = refusal(json.load(sys.stdin), args.lifecycle_dir)
    except (OSError, ValueError, TypeError, KeyError) as error:
        # A policy error is an explicit denial, not an accidentally fail-open hook.
        reason = f"Duplicate-execution guard cannot read original state: {error}"
    if reason is not None:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
