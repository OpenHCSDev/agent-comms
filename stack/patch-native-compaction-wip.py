#!/usr/bin/env python3
"""Carry the agent's stated work-in-progress lines verbatim into compaction.

The coordination instruction declares the ordered markers. This patch reads
that declaration to build its compaction reader; it owns no copied marker list.
Pi's one compaction engine runs for manual /compact, the threshold and
overflow recovery through the same _runDefaultCompaction; this patch makes
its result carry the agent's latest statement verbatim at the top of the
retained summary. The uncommitted-worktree fact is read from the thread's
own worktree at compaction start, so a stale statement cannot hide real
uncommitted changes. Run after patch-native-compaction-brief.py.
"""

import re
import sys
from pathlib import Path

INSTRUCTION = Path(__file__).resolve().parents[1] / "src/agent_comms/instructions/coordination.md"
WORK_IN_PROGRESS_MARKERS = tuple(re.findall(r"^`([A-Za-z]+):`", INSTRUCTION.read_text(), re.M))

# The declared triple as one line pattern; the same source text is valid as a
# Python regular expression and as the body of a JavaScript regex literal.
WORK_IN_PROGRESS_PATTERN = (
    r"(?:^|\n)"
    + r"\n".join(rf"{marker}: [^\n]*" for marker in WORK_IN_PROGRESS_MARKERS)
    + r"(?=\n|$)"
)
UNCOMMITTED_MARKER = WORK_IN_PROGRESS_MARKERS[1]

RETURN_ANCHOR = (
    "        return compact(preparation, requestModel, apiKey, headers, customInstructions, signal, "
    "this.thinkingLevel, this.agent.streamFunction, env, this.settingsManager.getRetrySettings(), "
    'this._summarizationRetryCallbacks({ source: "compaction", reason }), undefined);'
)
RETURN_CARRIED = (
    "        const result = await compact(preparation, requestModel, apiKey, headers, customInstructions, "
    "signal, this.thinkingLevel, this.agent.streamFunction, env, this.settingsManager.getRetrySettings(), "
    'this._summarizationRetryCallbacks({ source: "compaction", reason }), undefined);\n'
    "        result.summary = [await workInProgressLines(preparation, this._cwd), result.summary]"
    '.filter(Boolean).join("\\n\\n");\n'
    "        return result;"
)
CLASS_ANCHOR = "export class AgentSession {"
HELPER = f'''async function workInProgressLines(preparation, cwd) {{
    // The agent's last stated three lines survive compaction verbatim at the
    // top of the retained context; the uncommitted fact is read from the
    // worktree itself at compaction start, never from a side store.
    const messages = [
        ...(preparation?.messagesToSummarize ?? []),
        ...(preparation?.turnPrefixMessages ?? []),
    ];
    let stated = null;
    for (let index = messages.length - 1; index >= 0 && stated === null; index -= 1) {{
        const message = messages[index];
        if (message?.role !== 'assistant') continue;
        const blocks = Array.isArray(message.content) ? message.content : (
            typeof message.content === 'string'
                ? [{{ type: 'text', text: message.content }}]
                : []
        );
        const text = blocks
            .filter(block => block?.type === 'text')
            .map(block => block.text ?? '')
            .join('\\n');
        const matches = [...text.matchAll(/{WORK_IN_PROGRESS_PATTERN}/g)];
        if (matches.length > 0) stated = matches.at(-1)[0].trim();
    }}
    let dirty = false;
    try {{
        const childProcess = await import('node:child_process');
        dirty = childProcess.execFileSync('git', ['status', '--porcelain'], {{
            cwd,
            stdio: ['ignore', 'pipe', 'ignore'],
        }}).toString().trim().length > 0;
    }} catch {{
        // Outside a worktree there is no uncommitted fact to carry.
    }}
    if (!stated && !dirty) return '';
    const lines = ['Work in progress when compaction started:'];
    if (stated) {{
        for (const line of stated.split('\\n')) {{
            lines.push(
                dirty && line.startsWith('{UNCOMMITTED_MARKER}:')
                    ? line + ' (the worktree has uncommitted changes at compaction)'
                    : line,
            );
        }}
    }}
    if (dirty && !stated) {{
        lines.push('{UNCOMMITTED_MARKER}: the worktree has uncommitted changes at compaction.');
    }}
    return lines.join('\\n');
}}

'''


def main(session: Path) -> None:
    source = session.read_text()
    if source.count(RETURN_ANCHOR) != 1:
        raise SystemExit("Native compaction summary call changed")
    if source.count(CLASS_ANCHOR) != 1:
        raise SystemExit("Native agent session declaration changed")
    source = source.replace(RETURN_ANCHOR, RETURN_CARRIED, 1)
    source = source.replace(CLASS_ANCHOR, HELPER + CLASS_ANCHOR, 1)
    session.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
