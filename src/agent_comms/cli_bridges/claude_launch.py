"""Start Claude Code with the deal in its system prompt.

Core prepares one system prompt (deal_prompt.DealPrompt) and every launcher that can put a file
in its runtime's system prompt passes it. Claude Code's strongest channel is
--append-system-prompt-file: it lands in the system role after Claude Code's own prompt, where a
project CLAUDE.md would only arrive as user-side context. The launcher also names the prompt in
the environment Claude Code passes to every hook (LaunchedPrompt), so the deal knows the
session holds it and asks for the audit, not the reads.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from agent_comms.native_package import SYSTEM_PROMPT
from agent_comms_hooks import LaunchedPrompt


def native_claude() -> Path:
    """The first `claude` on PATH that is not this launcher: the user's `claude` may point here."""
    launcher = Path(sys.argv[0]).resolve()
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        found = shutil.which("claude", path=directory)
        if found is not None and Path(found).resolve() != launcher:
            return Path(found)
    raise FileNotFoundError("Install Claude Code before starting it through claude-comms")


def main() -> None:
    prompt = SYSTEM_PROMPT.resolve(strict=True)
    native = native_claude()
    environment = {**os.environ, LaunchedPrompt.variable: str(prompt)}
    os.execve(native, [str(native), "--append-system-prompt-file", str(prompt), *sys.argv[1:]],
              environment)
