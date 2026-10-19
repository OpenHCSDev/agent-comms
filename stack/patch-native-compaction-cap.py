#!/usr/bin/env python3
"""Make Pi's existing compaction budget the hard Core context policy.

``CompactionSettings`` already owns the retained payload budgets used by
threshold reservation, summary preparation, and recovery. Extend that owner
with one immutable 300,000-token retained-context threshold. SettingsManager
derives every managed session's effective settings from it, so a stale or
per-thread ``compaction.enabled: false`` cannot bypass the Core policy.

Pi still owns when and how compaction runs: ``shouldCompact`` is its existing
threshold decision, and AgentSession invokes it only at completed input/tool
boundaries before the next prompt. A completed answer settles before threshold
compaction, so its publisher cannot be held behind summary generation. Overflow
still recovers the interrupted request. Manual compaction remains unchanged. Model metadata
keeps reporting each provider's actual advertised window.
"""

import sys
from pathlib import Path


COMPACTION_JS_STOCK = """export const DEFAULT_COMPACTION_SETTINGS = {
    enabled: true,
    reserveTokens: 16384,
    keepRecentTokens: 20000,
};"""
COMPACTION_JS_OWNED = """// Core's retained-context payload budget. This is the sole declaration of the
// global threshold; model context windows remain provider metadata.
export const DEFAULT_COMPACTION_SETTINGS = Object.freeze({
    enabled: true,
    reserveTokens: 16384,
    keepRecentTokens: 20000,
    retainedContextTokens: 300000,
});"""

SHOULD_COMPACT_STOCK = """export function shouldCompact(contextTokens, contextWindow, settings) {
    if (!settings.enabled)
        return false;
    return contextTokens > contextWindow - settings.reserveTokens;
}"""
SHOULD_COMPACT_OWNED = """export function shouldCompact(contextTokens, contextWindow, settings) {
    if (!settings.enabled)
        return false;
    return contextTokens > Math.min(
        contextWindow - settings.reserveTokens,
        settings.retainedContextTokens,
    );
}"""

COMPACTION_DTS_STOCK = """export interface CompactionSettings {
    enabled: boolean;
    reserveTokens: number;
    keepRecentTokens: number;
}
export declare const DEFAULT_COMPACTION_SETTINGS: CompactionSettings;"""
COMPACTION_DTS_OWNED = """export interface CompactionSettings {
    enabled: boolean;
    reserveTokens: number;
    keepRecentTokens: number;
    /** Hard upper threshold for context retained between model requests. */
    retainedContextTokens: number;
}
export declare const DEFAULT_COMPACTION_SETTINGS: Readonly<CompactionSettings>;"""

SETTINGS_IMPORT_ANCHOR = 'import { DEFAULT_HTTP_IDLE_TIMEOUT_MS, parseHttpIdleTimeoutMs } from "./http-dispatcher.js";'
SETTINGS_IMPORT_OWNED = (
    SETTINGS_IMPORT_ANCHOR
    + '\nimport { DEFAULT_COMPACTION_SETTINGS } from "./compaction/compaction.js";'
)
SETTINGS_METHODS_STOCK = """    getCompactionEnabled() {
        return this.settings.compaction?.enabled ?? true;
    }
    setCompactionEnabled(enabled) {
        if (!this.globalSettings.compaction) {
            this.globalSettings.compaction = {};
        }
        this.globalSettings.compaction.enabled = enabled;
        this.markModified("compaction", "enabled");
        this.save();
    }
    getCompactionReserveTokens() {
        return this.settings.compaction?.reserveTokens ?? 16384;
    }
    getCompactionKeepRecentTokens() {
        return this.settings.compaction?.keepRecentTokens ?? 20000;
    }
    getCompactionSettings() {
        return {
            enabled: this.getCompactionEnabled(),
            reserveTokens: this.getCompactionReserveTokens(),
            keepRecentTokens: this.getCompactionKeepRecentTokens(),
        };
    }"""
SETTINGS_METHODS_OWNED = """    getCompactionEnabled() {
        return DEFAULT_COMPACTION_SETTINGS.enabled;
    }
    setCompactionEnabled(_enabled) {
        if (!this.globalSettings.compaction) {
            this.globalSettings.compaction = {};
        }
        this.globalSettings.compaction.enabled = DEFAULT_COMPACTION_SETTINGS.enabled;
        this.markModified("compaction", "enabled");
        this.save();
    }
    getCompactionReserveTokens() {
        return this.settings.compaction?.reserveTokens ?? DEFAULT_COMPACTION_SETTINGS.reserveTokens;
    }
    getCompactionKeepRecentTokens() {
        return this.settings.compaction?.keepRecentTokens ?? DEFAULT_COMPACTION_SETTINGS.keepRecentTokens;
    }
    getCompactionSettings() {
        return {
            enabled: this.getCompactionEnabled(),
            reserveTokens: this.getCompactionReserveTokens(),
            keepRecentTokens: this.getCompactionKeepRecentTokens(),
            retainedContextTokens: DEFAULT_COMPACTION_SETTINGS.retainedContextTokens,
        };
    }"""

SETTINGS_DTS_STOCK = """    getCompactionSettings(): {
        enabled: boolean;
        reserveTokens: number;
        keepRecentTokens: number;
    };"""
SETTINGS_DTS_OWNED = """    getCompactionSettings(): {
        enabled: boolean;
        reserveTokens: number;
        keepRecentTokens: number;
        retainedContextTokens: number;
    };"""

SUMMARY_JS_STOCK = """export async function generateSummaryWithUsage(currentMessages, model, reserveTokens, apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, streamFn, env, retry, callbacks, sessionId) {
    const maxTokens = Math.min(Math.floor(0.8 * reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);
    // Use update prompt if we have a previous summary, otherwise initial prompt
    let basePrompt = previousSummary ? UPDATE_SUMMARIZATION_PROMPT : SUMMARIZATION_PROMPT;
    if (customInstructions) {
        basePrompt = `${basePrompt}\\n\\nAdditional focus: ${customInstructions}`;
    }
    // Serialize conversation to text so model doesn't try to continue it
    // Convert to LLM messages first (handles custom types like bashExecution, custom, etc.)
    const llmMessages = convertToLlm(currentMessages);
    const conversationText = serializeConversation(llmMessages);
    // Build the prompt with conversation wrapped in tags
    let promptText = `<conversation>\\n${conversationText}\\n</conversation>\\n\\n`;
    if (previousSummary) {
        promptText += `<previous-summary>\\n${previousSummary}\\n</previous-summary>\\n\\n`;
    }
    promptText += basePrompt;
    const completionOptions = createSummarizationOptions(model, maxTokens, apiKey, headers, env, signal, thinkingLevel, sessionId);
    const response = await completeSummarization(model, buildSummarizationContext(promptText), completionOptions, streamFn, retry, callbacks);
    const failure = getSummarizationFailure(response, "Summarization");
    if (failure) {
        throw new Error(failure);
    }
    if (response.content.some((block) => block.type === "toolCall")) {
        throw new Error("Summarization attempted to call a tool");
    }
    const textContent = contentText(response.content);
    return { text: textContent, usage: response.usage };
}"""
SUMMARY_JS_OWNED = """function buildSummaryPrompt(conversationText, basePrompt, previousSummary) {
    let promptText = `<conversation>\\n${conversationText}\\n</conversation>\\n\\n`;
    if (previousSummary) {
        promptText += `<previous-summary>\\n${previousSummary}\\n</previous-summary>\\n\\n`;
    }
    return promptText + basePrompt;
}
function summaryInputTokenLimit(model, settings) {
    return Math.min(model.contextWindow - settings.reserveTokens, settings.retainedContextTokens);
}
async function generateBoundedSummary(conversationText, model, settings, apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, streamFn, env, retry, callbacks, sessionId, initialPrompt, updatePrompt, label, outputFraction) {
    const maxTokens = Math.min(Math.floor(outputFraction * settings.reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);
    const inputTokenLimit = summaryInputTokenLimit(model, settings);
    let remaining = conversationText;
    let summary = previousSummary;
    let usage;
    do {
        let basePrompt = summary ? updatePrompt : initialPrompt;
        if (customInstructions) {
            basePrompt = `${basePrompt}\\n\\nAdditional focus: ${customInstructions}`;
        }
        const emptyPrompt = buildSummaryPrompt("", basePrompt, summary);
        const availableChars = Math.floor(inputTokenLimit * 4 - SUMMARIZATION_SYSTEM_PROMPT.length - emptyPrompt.length);
        if (availableChars < 1) {
            throw new Error(`${label} cannot fit its instructions and previous summary within the retained-context payload budget`);
        }
        const conversationChunk = remaining.slice(0, availableChars);
        remaining = remaining.slice(conversationChunk.length);
        const promptText = buildSummaryPrompt(conversationChunk, basePrompt, summary);
        const completionOptions = createSummarizationOptions(model, maxTokens, apiKey, headers, env, signal, thinkingLevel, sessionId);
        const response = await completeSummarization(model, buildSummarizationContext(promptText), completionOptions, streamFn, retry, callbacks);
        const failure = getSummarizationFailure(response, label);
        if (failure) {
            throw new Error(failure);
        }
        if (response.content.some((block) => block.type === "toolCall")) {
            throw new Error(`${label} attempted to call a tool`);
        }
        summary = contentText(response.content);
        usage = usage ? combineUsage(usage, response.usage) : response.usage;
    } while (remaining.length > 0);
    return { text: summary, usage };
}
export async function generateSummaryWithUsage(currentMessages, model, reserveTokens, apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, streamFn, env, retry, callbacks, sessionId) {
    const settings = { ...DEFAULT_COMPACTION_SETTINGS, reserveTokens };
    const conversationText = serializeConversation(convertToLlm(currentMessages));
    return generateBoundedSummary(conversationText, model, settings, apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, streamFn, env, retry, callbacks, sessionId, SUMMARIZATION_PROMPT, UPDATE_SUMMARIZATION_PROMPT, "Summarization", 0.8);
}"""

TURN_PREFIX_JS_STOCK = """async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId) {
    const maxTokens = Math.min(Math.floor(0.5 * reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY); // Smaller budget for turn prefix
    const llmMessages = convertToLlm(messages);
    const conversationText = serializeConversation(llmMessages);
    const promptText = `<conversation>\\n${conversationText}\\n</conversation>\\n\\n${TURN_PREFIX_SUMMARIZATION_PROMPT}`;
    const response = await completeSummarization(model, buildSummarizationContext(promptText), createSummarizationOptions(model, maxTokens, apiKey, headers, env, signal, thinkingLevel, sessionId), streamFn, retry, callbacks);
    const failure = getSummarizationFailure(response, "Turn prefix summarization");
    if (failure) {
        throw new Error(failure);
    }
    if (response.content.some((block) => block.type === "toolCall")) {
        throw new Error("Turn prefix summarization attempted to call a tool");
    }
    return {
        text: contentText(response.content),
        usage: response.usage,
    };
}"""
TURN_PREFIX_JS_OWNED = """const UPDATE_TURN_PREFIX_SUMMARIZATION_PROMPT = `The messages above are the next part of a split turn prefix. Update the existing prefix summary in <previous-summary> so the retained suffix still has the original request, early progress, and necessary context. Keep the same Original Request, Early Progress, and Context for Suffix format.`;
async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId) {
    const settings = { ...DEFAULT_COMPACTION_SETTINGS, reserveTokens };
    const conversationText = serializeConversation(convertToLlm(messages));
    return generateBoundedSummary(conversationText, model, settings, apiKey, headers, signal, undefined, undefined, thinkingLevel, streamFn, env, retry, callbacks, sessionId, TURN_PREFIX_SUMMARIZATION_PROMPT, UPDATE_TURN_PREFIX_SUMMARIZATION_PROMPT, "Turn prefix summarization", 0.5);
}"""

SUCCESSFUL_OVERFLOW_STOCK = """            // Case 2: the response completed successfully. Compact, but do not retry because
            // agent.continue() cannot continue from a completed assistant response.
            if (!willRetry) {
                return await this._runAutoCompaction("overflow", false);
            }"""
SUCCESSFUL_OVERFLOW_OWNED = """            // A completed response is retained context, so crossing either the model-safe
            // threshold or Core's hard threshold is standard threshold compaction.
            // Overflow remains the reason only for an interrupted request that will retry.
            if (!willRetry) {
                return await this._runAutoCompaction("threshold", false);
            }"""


POST_RUN_OVERFLOW_STOCK = '''        const msg = this._lastAssistantMessage;
        this._lastAssistantMessage = undefined;
        if (!msg) {
            return false;
        }'''
POST_RUN_OVERFLOW_OWNED = POST_RUN_OVERFLOW_STOCK + '''
        const overflow = msg.stopReason === "error" &&
            isContextOverflow(msg, this.model?.contextWindow ?? 0);'''
TRACKED_COMPACTION_STOCK = '''            const overflow = msg.stopReason === "error" &&
                isContextOverflow(msg, this.model?.contextWindow ?? 0);
            if (msg.stopReason !== "stop" && !overflow) return false;
            if (await this._checkCompaction(msg)) return true;'''
TRACKED_COMPACTION_OWNED = '''            if (msg.stopReason !== "stop" && !overflow) return false;
            if (overflow && await this._checkCompaction(msg)) return true;'''
UNTRACKED_COMPACTION_STOCK = '''        if (await this._checkCompaction(msg)) {
            return true;
        }'''
UNTRACKED_COMPACTION_OWNED = '''        if (overflow && await this._checkCompaction(msg)) {
            return true;
        }'''
COMPACTION_FAILURE_STOCK = '''            return false;
        }
        finally {
            this._autoCompactionAbortController = undefined;'''
COMPACTION_FAILURE_OWNED = '''            throw error;
        }
        finally {
            this._autoCompactionAbortController = undefined;'''


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    if source.count(old) != 1:
        raise SystemExit(f"Native compaction owner changed in {path}; expected one anchor")
    path.write_text(source.replace(old, new, 1))


def main(package: Path) -> None:
    compaction = package / "dist/core/compaction/compaction.js"
    compaction_types = package / "dist/core/compaction/compaction.d.ts"
    settings = package / "dist/core/settings-manager.js"
    settings_types = package / "dist/core/settings-manager.d.ts"
    session = package / "dist/core/agent-session.js"

    replace_once(compaction, COMPACTION_JS_STOCK, COMPACTION_JS_OWNED)
    replace_once(compaction, SHOULD_COMPACT_STOCK, SHOULD_COMPACT_OWNED)
    replace_once(compaction_types, COMPACTION_DTS_STOCK, COMPACTION_DTS_OWNED)
    replace_once(settings, SETTINGS_IMPORT_ANCHOR, SETTINGS_IMPORT_OWNED)
    replace_once(settings, SETTINGS_METHODS_STOCK, SETTINGS_METHODS_OWNED)
    replace_once(settings_types, SETTINGS_DTS_STOCK, SETTINGS_DTS_OWNED)
    replace_once(compaction, SUMMARY_JS_STOCK, SUMMARY_JS_OWNED)
    replace_once(compaction, TURN_PREFIX_JS_STOCK, TURN_PREFIX_JS_OWNED)
    replace_once(session, SUCCESSFUL_OVERFLOW_STOCK, SUCCESSFUL_OVERFLOW_OWNED)
    # AgentSession.prompt already checks the threshold before the next input.
    # Post-run work owns recovery/queued continuation, not withholding a final answer.
    replace_once(session, POST_RUN_OVERFLOW_STOCK, POST_RUN_OVERFLOW_OWNED)
    replace_once(session, TRACKED_COMPACTION_STOCK, TRACKED_COMPACTION_OWNED)
    replace_once(session, UNTRACKED_COMPACTION_STOCK, UNTRACKED_COMPACTION_OWNED)
    replace_once(session, COMPACTION_FAILURE_STOCK, COMPACTION_FAILURE_OWNED)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
