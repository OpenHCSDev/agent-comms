#!/usr/bin/env python3
"""Expose original persisted rows and the SDK's completed-round frontier."""
from pathlib import Path
import sys


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f"Native output persistence seam changed: {path}")
    path.write_text(source.replace(before, after, 1))


core = Path(sys.argv[1]) / 'dist/core'
session = core / 'agent-session.js'
replace_once(session,
    '    _handleAgentEvent = async (event, signal) => {',
    '''    _nativeCommittedLocation(entryId) {
        const placement = this.sessionManager.entryStore.metadata(entryId);
        // In-memory sessions have no persisted prefix to certify.
        if (!this.sessionFile || !placement || placement.length <= 0) return undefined;
        return { sessionId: this.sessionId, sessionFile: this.sessionFile,
            sessionEntryId: entryId, sessionEntryStart: placement.offset,
            sessionEntryEnd: placement.offset + placement.length + 1 };
    }
    _handleAgentEvent = async (event, signal) => {
        if (event.type === "turn_end") {
            // The loop has persisted every result. Capture before extension
            // callbacks and the pending-custom-message flush below.
            event = { ...event,
                roundCommitted: this._nativeCommittedLocation(this.sessionManager.getLeafId()) };
        }''')
replace_once(session,
    '                const entryId = this.sessionManager.appendMessage(event.message);',
    '''                const entryId = this.sessionManager.appendMessage(event.message);
                if (event.message.role === "assistant" || event.message.role === "toolResult") {
                    const committed = this._nativeCommittedLocation(entryId);
                    if (committed) {
                        this._emit({ type: "native_output_committed", ...committed,
                            output: event.message.role === "assistant"
                                ? { kind: "assistant" }
                                : { kind: "tool", toolCallId: event.message.toolCallId },
                        });
                    }
                }''')
replace_once(core / 'agent-session.d.ts',
    'export type AgentSessionEvent = Exclude<AgentEvent, {\n    type: "agent_end";',
    '''export interface NativeCommittedLocation {
    sessionId: string;
    sessionFile: string;
    sessionEntryId: string;
    sessionEntryStart: number;
    sessionEntryEnd: number;
}
export type AgentSessionEvent = (NativeCommittedLocation & {
    type: "native_output_committed";
    output: { kind: "assistant" } | { kind: "tool"; toolCallId: string };
}) | (Extract<AgentEvent, { type: "turn_end" }> & {
    roundCommitted?: NativeCommittedLocation;
}) | Exclude<AgentEvent, {
    type: "agent_end" | "turn_end";''')
