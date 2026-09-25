#!/usr/bin/env python3
"""Run tracked-input threshold compaction without replay on the pinned Pi copy."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "424066056076cbe2861c12277cb745428b13bf3308a8b9873d3064b62dc740d7"
OLD = "            if (lastAssistant && !options?.inputId) {\n"
NEW = (
    "            // A prior successful response may compact before this new tracked input.\n"
    "            // Failed tracked attempts still require an explicit recovery decision.\n"
    '            if (lastAssistant && (!options?.inputId || lastAssistant.stopReason === "stop")) {\n'
)
THRESHOLD_OLD = """        if (this._nativeRunHadTrackedInput || !model ||
            model.contextWindow <= 0 ||
            !shouldCompact(estimateContextTokens(context.messages).tokens, model.contextWindow, settings)) {
            return context;
        }
        await this._runAutoCompaction("threshold", false);
        return {
            ...context,
            messages: this.agent.state.messages.slice(),
        };
"""
THRESHOLD_NEW = """        if (!model || model.contextWindow <= 0 ||
            !shouldCompact(estimateContextTokens(context.messages).tokens, model.contextWindow, settings)) {
            return context;
        }
        // This is before the next model send, including a tracked input's first
        // send and later tool continuations. An unsuccessful summary must stop
        // the turn here; never send the still oversized context to the model.
        await this._runAutoCompaction("threshold", false);
        const messages = this.agent.state.messages.slice();
        if (shouldCompact(estimateMessagesTokens(messages), model.contextWindow, settings)) {
            throw new Error("Automatic compaction did not reduce context enough; prompt was not sent.");
        }
        return { ...context, messages };
"""
COMPACTION_CALL_OLD = """        return compact(preparation, requestModel, apiKey, headers, customInstructions, signal, this.thinkingLevel, this.agent.streamFunction, env, this.settingsManager.getRetrySettings(), this._summarizationRetryCallbacks({ source: "compaction", reason }), undefined);
"""
COMPACTION_CALL_NEW = """        // Each summary chunk is one attempt. Pi's summarization retry helper and
        // the provider SDK must both be disabled, including pre-prompt compact.
        const oneAttempt = (model, context, options) =>
            this.agent.streamFunction(model, context, { ...options, maxRetries: 0 });
        const noRetry = { enabled: false, maxRetries: 0, baseDelayMs: 0 };
        return compact(preparation, requestModel, apiKey, headers, customInstructions, signal, this.thinkingLevel, oneAttempt, env, noRetry, this._summarizationRetryCallbacks({ source: "compaction", reason }), undefined);
"""
FIRST_SEND_OLD = """        if (!messages) {
            return;
        }
        try {
            preflightAccepted = true;
            preflightResult?.(true);
            await this._runAgentPrompt(messages);
"""
FIRST_SEND_NEW = """        if (!messages) {
            return;
        }
        try {
            // prepareNextTurn runs only after the first assistant response. A
            // tracked input needs this threshold check BEFORE its first model
            // send, including after a previous error with no usable usage.
            if (options?.inputId && this.model?.contextWindow > 0) {
                const settings = this.settingsManager.getCompactionSettings();
                const pendingContext = () => [...this.agent.state.messages, ...messages];
                if (shouldCompact(estimateMessagesTokens(pendingContext()), this.model.contextWindow, settings)) {
                    await this._runAutoCompaction("threshold", false);
                    if (shouldCompact(estimateMessagesTokens(pendingContext()), this.model.contextWindow, settings)) {
                        throw new Error("Automatic compaction did not reduce context enough; prompt was not sent.");
                    }
                }
            }
            preflightAccepted = true;
            preflightResult?.(true);
            await this._runAgentPrompt(messages);
"""
BRANCH_SUMMARY_OLD = """                    streamFn: this.agent.streamFunction,
                    retry: this.settingsManager.getRetrySettings(),
                    callbacks: this._summarizationRetryCallbacks({ source: "branchSummary" }),
"""
BRANCH_SUMMARY_NEW = """                    // Branch summaries also make provider calls. A dropped
                    // response is uncertain and cannot be retried automatically.
                    streamFn: (model, context, options) =>
                        this.agent.streamFunction(model, context, { ...options, maxRetries: 0 }),
                    retry: { enabled: false, maxRetries: 0, baseDelayMs: 0 },
                    callbacks: this._summarizationRetryCallbacks({ source: "branchSummary" }),
"""


def main(path: Path) -> None:
    before = path.read_bytes()
    if hashlib.sha256(before).hexdigest() != BASE_SHA:
        raise SystemExit("Native session source does not match the pinned Pi build")
    source = before.decode()
    if any(
        source.count(anchor) != 1
        for anchor in (
            OLD,
            THRESHOLD_OLD,
            COMPACTION_CALL_OLD,
            FIRST_SEND_OLD,
            BRANCH_SUMMARY_OLD,
        )
    ):
        raise SystemExit("Native compaction anchors changed")
    source = source.replace(OLD, NEW, 1)
    source = source.replace(THRESHOLD_OLD, THRESHOLD_NEW, 1)
    source = source.replace(COMPACTION_CALL_OLD, COMPACTION_CALL_NEW, 1)
    source = source.replace(FIRST_SEND_OLD, FIRST_SEND_NEW, 1)
    source = source.replace(BRANCH_SUMMARY_OLD, BRANCH_SUMMARY_NEW, 1)
    path.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
