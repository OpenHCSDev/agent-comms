#!/usr/bin/env python3
"""Keep native compaction bounded and report progress on the pinned Pi copy."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_SHA = "424066056076cbe2861c12277cb745428b13bf3308a8b9873d3064b62dc740d7"
INSTALL_ANCHOR = "        this._installAgentNextTurnRefresh();\n"
INSTALL = "        this._installNativeCompactionBeforeProvider();\n"
METHOD_ANCHOR = "    _installAgentNextTurnRefresh() {\n"
METHOD = """    _installNativeCompactionBeforeProvider() {
        const previous = this.agent.transformContext;
        this.agent.transformContext = async (messages, signal) => {
            const transformed = previous ? await previous.call(this.agent, messages, signal) : messages;
            if (!this._nativeRunHadTrackedInput || !this.model || this.model.contextWindow <= 0)
                return transformed;
            const settings = this.settingsManager.getCompactionSettings();
            // Retained assistant usage still measures the old full context until
            // a response follows the latest compaction. Use Pi's authoritative
            // usage validity check before consulting that saved measurement.
            const contextTokens = this.getContextUsage()?.tokens === null
                ? estimateMessagesTokens(transformed)
                : estimateContextTokens(transformed).tokens;
            if (!shouldCompact(contextTokens, this.model.contextWindow, settings))
                return transformed;
            const budget = this.model.contextWindow - settings.reserveTokens;
            const currentInput = transformed.findLast((message) => message.role === "user" &&
                this._nativeInputClaims.has(message.inputId));
            if (currentInput && estimateTokens(currentInput) > budget)
                throw new Error("Native input is oversized and cannot be compacted; prompt refused");
            const before = getLatestCompactionEntry(this.sessionManager.getBranch());
            await this._runAutoCompaction("threshold", false);
            const after = getLatestCompactionEntry(this.sessionManager.getBranch());
            if (!after || after.id === before?.id)
                throw new Error("Native threshold compaction did not commit; prompt refused");
            const fresh = this.agent.state.messages.slice();
            const finalMessages = previous ? await previous.call(this.agent, fresh, signal) : fresh;
            if (shouldCompact(estimateMessagesTokens(finalMessages), this.model.contextWindow, settings))
                throw new Error("Native threshold compaction left an oversized context; prompt refused");
            return finalMessages;
        };
    }
"""
COMPACTION_CALL = (
    "        return compact(preparation, requestModel, apiKey, headers, customInstructions, "
    "signal, this.thinkingLevel, this.agent.streamFunction, env, "
    "this.settingsManager.getRetrySettings(), "
    'this._summarizationRetryCallbacks({ source: "compaction", reason }), undefined);'
)
BOUNDED_COMPACTION_CALL = (
    "        let responseIndex = 0;\n"
    "        // Summary work uses low reasoning even when the user turn requests high reasoning.\n"
    '        const callbacks = { ...this._summarizationRetryCallbacks({ source: "compaction", reason }), '
    'onSummaryStart: (progress) => this._emit({ type: "compaction_progress", reason, chunkIndex: responseIndex, ...progress }), '
    'onSummaryResponse: (usage, progress) => this._emit({ type: "compaction_progress", reason, '
    "chunkIndex: ++responseIndex, usage, ...progress }) };\n"
    "        return compact(preparation, requestModel, apiKey, headers, customInstructions, "
    'signal, "low", this.agent.streamFunction, env, '
    "{ enabled: false, maxRetries: 0, provider: { maxRetries: 0 } }, callbacks, undefined);"
)
BRANCH_RETRY = "                    retry: this.settingsManager.getRetrySettings(),\n"
NO_RETRY = (
    "                    retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },\n"
)


def main(path: Path) -> None:
    before = path.read_bytes()
    if hashlib.sha256(before).hexdigest() != BASE_SHA:
        raise SystemExit("Native session source does not match the pinned Pi build")
    source = before.decode()
    if (
        source.count(INSTALL_ANCHOR) != 1
        or source.count(METHOD_ANCHOR) != 1
        or source.count(COMPACTION_CALL) != 1
        or source.count(BRANCH_RETRY) != 1
    ):
        raise SystemExit("Native compaction anchor changed")
    path.write_text(
        source.replace(INSTALL_ANCHOR, INSTALL_ANCHOR + INSTALL, 1)
        .replace(METHOD_ANCHOR, METHOD + METHOD_ANCHOR, 1)
        .replace(COMPACTION_CALL, BOUNDED_COMPACTION_CALL, 1)
        .replace(BRANCH_RETRY, NO_RETRY, 1)
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
