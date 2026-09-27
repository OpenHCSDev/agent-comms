#!/usr/bin/env python3
"""Pinned Pi RPC selected-summary patch, after native readiness.

Normal preparation includes this operation for the existing selected owner.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "4c42e792577d25d9a97a13c9b7d82959d2f90aa05938409f54f0a220326d453e"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Pinned selected-summary anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise SystemExit("Selected summary requires exact phase1-patched RPC bytes")
    source = raw.decode()
    source = replace_once(source,
        'import { prepareCompaction } from "../../core/compaction/index.js";',
        'import { compact, prepareCompaction, serializeConversation, shouldCompact } from "../../core/compaction/index.js";\n'
        'import { convertToLlm } from "../../core/messages.js";\n'
        'import { AssistantMessageEventStream } from "../../../node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js";')
    helper = Path(__file__).with_name("native-compaction-selected-summary.mjs").read_text()
    source = replace_once(source, "export async function runRpcMode(runtimeHost) {", helper + "\nexport async function runRpcMode(runtimeHost) {")
    source = replace_once(source,
        "    let acOtherCommandInFlight = 0;\n    // Handle a single command",
        "    let acOtherCommandInFlight = 0;\n"
        "    let acSummarySlot = null;\n"
        "    const acSpentSummaryIds = new Set();\n"
        "    // Handle a single command")
    source = replace_once(source,
        '''        const id = command.id;
        switch (command.type) {''',
        '''        const id = command.id;
        // The phase2 slot is reserved synchronously before ANY await. RPC stdin
        // dispatches concurrent lines, so native mutations cannot interleave.
        if (acSummarySlot && !["agent_comms_cancel_summary", "agent_comms_summarize_compaction",
            "agent_comms_prepare_compaction", "agent_comms_compaction_settings", "get_state"].includes(command.type))
            return error(id, command.type, "Selected summary in flight; mutation denied");
        switch (command.type) {''')
    source = replace_once(source, '''            case "agent_comms_prepare_compaction": {''', '''            case "agent_comms_compaction_settings": {
                if (!acValidCompactionSettingsRequest(command))
                    return error(id, command.type, "Invalid selected compaction settings request");
                return success(id, command.type, acSelectedCompactionSettings(command, session,
                    acSummarySlot !== null || acOtherCommandInFlight !== 0));
            }
            case "agent_comms_summarize_compaction": {
                if (!acValidSummaryRequest(command))
                    return error(id, command.type, "Invalid v1 selected-summary request");
                const admission = acAdmitSummary(command, session,
                    acSummarySlot !== null || acOtherCommandInFlight !== 0,
                    acSpentSummaryIds, runtimeHost);
                if (admission.denial)
                    return success(id, command.type, acSummaryDecline(command.operationId, admission.denial));
                const selectedSession = session;
                const slot = { operationId: command.operationId, controller: new AbortController(),
                    started: false, timedOut: false, done: null };
                acSummarySlot = slot;
                acSpentSummaryIds.add(command.operationId);
                slot.done = acExecuteSummary(slot, selectedSession, command,
                    admission.preparation, admission.binding).finally(() => {
                    if (acSummarySlot === slot) acSummarySlot = null;
                });
                return success(id, command.type, await slot.done);
            }
            case "agent_comms_cancel_summary": {
                if (!acValidSummaryCancel(command))
                    return error(id, command.type, "Invalid v1 selected-summary cancellation");
                const slot = acSummarySlot;
                if (!slot || slot.operationId !== command.operationId)
                    return success(id, command.type, acSpentSummaryIds.has(command.operationId)
                        ? acSummaryUnknown(command.operationId)
                        : acSummaryDecline(command.operationId, "unsupported"));
                slot.controller.abort();
                await slot.done; // NEVER free the slot before all concurrent chunks join.
                return success(id, command.type, slot.started
                    ? acSummaryUnknown(command.operationId)
                    : acSummaryDecline(command.operationId, "cancelled"));
            }
            case "agent_comms_prepare_compaction": {''')
    source = replace_once(source,
        "acPrepareReadiness(command, session, acOtherCommandInFlight !== 0)",
        "acPrepareReadiness(command, session, acOtherCommandInFlight !== 0 || acSummarySlot !== null)")
    source = replace_once(source,
        "isCompacting: session.isCompacting,",
        "isCompacting: session.isCompacting || acSummarySlot !== null,")
    source = replace_once(source,
        '''        const acCountCommand = command?.type !== "agent_comms_prepare_compaction" &&
            command?.type !== "get_state";''',
        '''        const acCountCommand = !["agent_comms_prepare_compaction", "agent_comms_summarize_compaction",
            "agent_comms_cancel_summary", "agent_comms_compaction_settings", "get_state"].includes(command?.type);''')
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
