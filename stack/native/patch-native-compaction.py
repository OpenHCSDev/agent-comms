#!/usr/bin/env python3
"""Keep Pi summaries bounded and settle completed answers before compaction.

Pi's unmodified SettingsManager and shouldCompact own automatic policy:
compaction.enabled, reserveTokens, keepRecentTokens and the model context window.
This patch only shares bounded summary generation and preserves input/recovery
lifetime across manual, threshold and overflow compaction.
"""

import sys
from pathlib import Path


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
async function generateBoundedSummary(conversationText, model, settings, apiKey, headers, signal, customInstructions, previousSummary, thinkingLevel, streamFn, env, retry, callbacks, sessionId, initialPrompt, updatePrompt, label, outputFraction) {
    const maxTokens = Math.min(Math.floor(outputFraction * settings.reserveTokens), model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY);
    const inputTokenLimit = model.contextWindow - settings.reserveTokens;
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
        signal?.throwIfAborted();
        if (response.stopReason === "aborted")
            throw new DOMException("Compaction cancelled", "AbortError");
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
SUCCESSFUL_OVERFLOW_OWNED = """            // A completed response is retained context; crossing the model-safe
            // threshold is standard threshold compaction.
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


# Cancellation is not the no-work result: callers must not resume an unchanged
# compulsory context after aborting summary generation.
EXTENSION_CANCEL_STOCK = '''                if (extensionResult?.cancel) {
                    this._emit({
                        type: "compaction_end",
                        reason,
                        result: undefined,
                        aborted: true,
                        willRetry: false,
                    });
                    await this._emitSessionCompactFailed({
                        reason,
                        aborted: true,
                        willRetry: false,
                        fromExtension: false,
                    });
                    return false;
                }
'''
EXTENSION_CANCEL_OWNED = '''                if (extensionResult?.cancel) {
                    throw new DOMException("Compaction cancelled", "AbortError");
                }
'''

AUTO_CANCEL_STOCK = '''            if (this._autoCompactionAbortController.signal.aborted) {
                this._emit({
                    type: "compaction_end",
                    reason,
                    result: undefined,
                    aborted: true,
                    willRetry: false,
                });
                await this._emitSessionCompactFailed({
                    reason,
                    aborted: true,
                    willRetry: false,
                    fromExtension,
                });
                return false;
            }
'''
AUTO_CANCEL_OWNED = '''            this._autoCompactionAbortController.signal.throwIfAborted();
'''

AUTO_FAILURE_STOCK = '''        catch (error) {
            const errorMessage = error instanceof Error ? error.message : "compaction failed";
            if (started) {
                const formattedErrorMessage = reason === "overflow"
                    ? `Context overflow recovery failed: ${errorMessage}`
                    : `Auto-compaction failed: ${errorMessage}`;
                this._emit({
                    type: "compaction_end",
                    reason,
                    result: undefined,
                    aborted: false,
                    willRetry: false,
                    errorMessage: formattedErrorMessage,
                });
                await this._emitSessionCompactFailed({
                    reason,
                    errorMessage: formattedErrorMessage,
                    aborted: false,
                    willRetry: false,
                    fromExtension,
                });
            }
            throw error;
        }
'''
AUTO_FAILURE_OWNED = '''        catch (error) {
            const aborted = this._autoCompactionAbortController?.signal.aborted ||
                (error instanceof Error && error.name === "AbortError");
            if (aborted && reason === "overflow") {
                this._overflowRecoveryAttempted = false;
                this.reloadContextMessages();
            }
            const errorMessage = error instanceof Error ? error.message : "compaction failed";
            if (started) {
                const formattedErrorMessage = aborted ? undefined : reason === "overflow"
                    ? `Context overflow recovery failed: ${errorMessage}`
                    : `Auto-compaction failed: ${errorMessage}`;
                this._emit({
                    type: "compaction_end",
                    reason,
                    result: undefined,
                    aborted,
                    willRetry: false,
                    errorMessage: formattedErrorMessage,
                });
                await this._emitSessionCompactFailed({
                    reason,
                    errorMessage: formattedErrorMessage,
                    aborted,
                    willRetry: false,
                    fromExtension,
                });
            }
            throw error;
        }
'''


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    if source.count(old) != 1:
        raise SystemExit(f"Native compaction owner changed in {path}; expected one anchor")
    path.write_text(source.replace(old, new, 1))


def main(package: Path) -> None:
    compaction = package / "dist/core/compaction/compaction.js"
    session = package / "dist/core/agent-session.js"

    replace_once(compaction, SUMMARY_JS_STOCK, SUMMARY_JS_OWNED)
    replace_once(compaction, TURN_PREFIX_JS_STOCK, TURN_PREFIX_JS_OWNED)
    # A split can discard only a turn prefix, with no new complete history.
    # Keep the existing history summary when there is nothing to resummarize.
    replace_once(compaction, 'let historyText = "No prior history.";',
                 'let historyText = previousSummary ?? "No prior history.";')
    replace_once(session, SUCCESSFUL_OVERFLOW_STOCK, SUCCESSFUL_OVERFLOW_OWNED)
    # AgentSession.prompt already checks the threshold before the next input.
    # Post-run work owns recovery/queued continuation, not withholding a final answer.
    replace_once(session, POST_RUN_OVERFLOW_STOCK, POST_RUN_OVERFLOW_OWNED)
    replace_once(session, TRACKED_COMPACTION_STOCK, TRACKED_COMPACTION_OWNED)
    replace_once(session, UNTRACKED_COMPACTION_STOCK, UNTRACKED_COMPACTION_OWNED)
    replace_once(session, COMPACTION_FAILURE_STOCK, COMPACTION_FAILURE_OWNED)
    replace_once(session, EXTENSION_CANCEL_STOCK, EXTENSION_CANCEL_OWNED)
    replace_once(session, AUTO_CANCEL_STOCK, AUTO_CANCEL_OWNED)
    replace_once(session, AUTO_FAILURE_STOCK, AUTO_FAILURE_OWNED)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
