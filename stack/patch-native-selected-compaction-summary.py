#!/usr/bin/env python3
"""Install selected-owner compaction and its mutation fence in pinned Pi RPC."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "bd6dfca7b14cad4023c5ab56a7fc91bef3db9670c96b6ad7625df16353b42e5a"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Pinned selected-summary anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(path: Path) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise SystemExit("Selected summary requires exact pinned RPC bytes")
    source = raw.decode()
    source = replace_once(source,
        'import * as crypto from "node:crypto";',
        'import * as crypto from "node:crypto";\n'
        'import { CompactionPolicy } from "../../core/compaction/agent-comms-policy.js";\n'
        'import { SessionContext } from "../../core/session-context.js";\n'
        'import { compact, prepareCompaction } from "../../core/compaction/index.js";\n'
        'import { AssistantMessageEventStream } from "../../../node_modules/@earendil-works/pi-ai/dist/utils/event-stream.js";')
    helper = Path(__file__).with_name("native-compaction-selected-summary.mjs").read_text()
    source = replace_once(source, "export async function runRpcMode(runtimeHost) {", helper + "\nexport async function runRpcMode(runtimeHost) {")
    source = replace_once(source,
        "    // Handle a single command\n    const handleCommand",
        "    let acOtherCommandInFlight = 0;\n"
        "    let acSummarySlot = null;\n"
        "    const acSpentSummaryIds = new Set();\n"
        "    // Handle a single command\n    const handleCommand")
    source = replace_once(source,
        '''        const id = command.id;
        switch (command.type) {''',
        '''        const id = command.id;
        // Reserve synchronously before ANY await: RPC dispatches concurrent lines.
        if (acSummarySlot && !["agent_comms_cancel_summary", "agent_comms_summarize_compaction",
            "agent_comms_compaction_settings", "agent_comms_prepare_compaction", "get_state"].includes(command.type))
            return error(id, command.type, "Selected summary in flight; mutation denied");
        switch (command.type) {''')
    source = replace_once(source, '''                void session
                    .prompt(command.message, {''', '''                // Prompt preflight may await auth/extensions before isStreaming.
                // Retain the mutation fence until the entire promise settles.
                acOtherCommandInFlight++;
                void session
                    .prompt(command.message, {''')
    source = replace_once(source, '''                    .catch((e) => {
                    if (!preflightSucceeded) {
                        output(error(id, "prompt", e.message));
                    }
                });''', '''                    .catch((e) => {
                    if (!preflightSucceeded) {
                        output(error(id, "prompt", e.message));
                    }
                })
                    .finally(() => { acOtherCommandInFlight--; });''')
    source = replace_once(source, '''            case "get_state": {''', '''            case "agent_comms_compaction_settings": {
                if (!acValidCompactionSettingsRequest(command))
                    return error(id, command.type, "Invalid selected compaction settings request");
                return success(id, command.type, acSelectedCompactionSettings(command, session,
                    acSummarySlot !== null || acOtherCommandInFlight !== 0));
            }
            case "agent_comms_prepare_compaction": {
                if (!acValidCompactionPreparationRequest(command))
                    return error(id, command.type, "Invalid selected preparation request");
                return success(id, command.type, acSelectedCompactionPreparation(command, session,
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
                    started: false, done: null };
                acSummarySlot = slot;
                acSpentSummaryIds.add(command.operationId);
                slot.done = acExecuteSummary(slot, selectedSession, command,
                    admission.preparation, admission.binding, output).finally(() => {
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
            case "get_state": {''')
    source = replace_once(source,
        "isCompacting: session.isCompacting,",
        "isCompacting: session.isCompacting || acSummarySlot !== null,")
    source = replace_once(source, '''        const command = parsed;
        try {
            const response = await handleCommand(command);''', '''        const command = parsed;
        const acCountCommand = !["agent_comms_summarize_compaction", "agent_comms_cancel_summary",
            "agent_comms_compaction_settings", "agent_comms_prepare_compaction", "get_state"].includes(command?.type);
        if (acCountCommand) acOtherCommandInFlight++;
        try {
            const response = await handleCommand(command);''')
    source = replace_once(source, '''            output(error(command.id, command.type, commandError instanceof Error ? commandError.message : String(commandError)));
            await waitForRawStdoutBackpressure();
        }
    };''', '''            output(error(command?.id, command?.type ?? "parse", commandError instanceof Error ? commandError.message : String(commandError)));
            await waitForRawStdoutBackpressure();
        }
        finally {
            if (acCountCommand) acOtherCommandInFlight--;
        }
    };''')
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
