#!/usr/bin/env python3
"""Publish each original output only after SessionManager appended its record."""
from pathlib import Path
import sys


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f"Native output persistence seam changed: {path}")
    path.write_text(source.replace(before, after, 1))


core = Path(sys.argv[1]) / 'dist/core'
replace_once(core / 'agent-session.js',
    '                const entryId = this.sessionManager.appendMessage(event.message);',
    '''                const entryId = this.sessionManager.appendMessage(event.message);
                if (event.message.role === "assistant" || event.message.role === "toolResult") {
                    const placement = this.sessionManager.entryStore.metadata(entryId);
                    // In-memory sessions have no persisted prefix to certify.
                    if (this.sessionFile && placement.length > 0) {
                        this._emit({ type: "native_output_committed",
                            sessionId: this.sessionId, sessionFile: this.sessionFile,
                            sessionEntryId: entryId, sessionEntryStart: placement.offset,
                            sessionEntryEnd: placement.offset + placement.length + 1,
                            output: event.message.role === "assistant"
                                ? { kind: "assistant" }
                                : { kind: "tool", toolCallId: event.message.toolCallId },
                        });
                    }
                }''')
replace_once(core / 'agent-session.d.ts',
    'export type AgentSessionEvent = ',
    '''export type AgentSessionEvent = {
    type: "native_output_committed";
    sessionId: string;
    sessionFile: string;
    sessionEntryId: string;
    sessionEntryStart: number;
    sessionEntryEnd: number;
    output: { kind: "assistant" } | { kind: "tool"; toolCallId: string };
} | ''')
