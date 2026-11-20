#!/usr/bin/env python3
"""Bound complete tool results before they enter Pi's persistent model context."""
import sys
from pathlib import Path


def replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f"Native tool-result budget source changed: {path}")
    path.write_text(source.replace(before, after, 1))


def main(package: str) -> None:
    root = Path(package)
    session = root / "dist/core/agent-session.js"
    module = root / "dist/core/native-tool-result-budget.js"
    module.write_bytes(Path(__file__).with_name("native-tool-result-budget.mjs").read_bytes())

    replace_once(
        session,
        'import { InactiveNativeTurnRun, NativeTurnRun } from "./turn-recovery.js";\n',
        'import { InactiveNativeTurnRun, NativeTurnRun } from "./turn-recovery.js";\n'
        'import { ToolResultArtifactStore, ToolResultCompactionState, budgetToolResult } from "./native-tool-result-budget.js";\n',
    )
    replace_once(
        session,
        "    _overflowRecoveryAttempted = false;\n",
        "    _overflowRecoveryAttempted = false;\n"
        "    _toolResultCompactionState = ToolResultCompactionState.shared;\n"
        "    _toolResultArtifactStore;\n",
    )
    replace_once(
        session,
        "        this._modelRuntime = config.modelRuntime;\n",
        "        this._modelRuntime = config.modelRuntime;\n"
        "        this._toolResultArtifactStore = new ToolResultArtifactStore(this.sessionId);\n",
    )
    replace_once(
        session,
        """        this.agent.afterToolCall = async ({ toolCall, args, result, isError }) => {
            const runner = this._extensionRunner;
            const hookResult = runner.hasHandlers("tool_result")
                ? await runner.emitToolResult({
                    type: "tool_result",
                    toolName: toolCall.name,
                    toolCallId: toolCall.id,
                    input: args,
                    content: result.content,
                    details: result.details,
                    isError,
                    usage: result.usage,
                })
                : undefined;
            const content = hookResult?.content ?? result.content ?? [];
            // Runs after the extension hook so images injected or replaced by extensions are normalized too.
            const normalizedContent = await normalizeToolResultImages(content, {
                autoResizeImages: this.settingsManager.getImageAutoResize(),
            });
            if (!hookResult && normalizedContent === content) {
                return undefined;
            }
            return {
                content: normalizedContent,
                details: hookResult?.details,
                isError: hookResult?.isError ?? isError,
                usage: hookResult?.usage,
            };
        };
""",
        """        this.agent.afterToolCall = async ({ assistantMessage, toolCall, args, result, isError, context }) => {
            const runner = this._extensionRunner;
            const hookResult = runner.hasHandlers("tool_result")
                ? await runner.emitToolResult({
                    type: "tool_result",
                    toolName: toolCall.name,
                    toolCallId: toolCall.id,
                    input: args,
                    content: result.content,
                    details: result.details,
                    isError,
                    usage: result.usage,
                })
                : undefined;
            const content = hookResult?.content ?? result.content ?? [];
            // Runs after the extension hook so images injected or replaced by extensions are normalized too.
            const normalizedContent = await normalizeToolResultImages(content, {
                autoResizeImages: this.settingsManager.getImageAutoResize(),
            });
            const settings = this.settingsManager.getCompactionSettings();
            const budgeted = await budgetToolResult({
                toolCall,
                content: normalizedContent,
                details: hookResult?.details ?? result.details,
                contextTokens: estimateContextTokens(context.messages).tokens,
                contextWindow: this.model?.contextWindow ?? 0,
                reserveTokens: settings.reserveTokens,
                parallelCalls: assistantMessage.content.filter(block => block.type === "toolCall").length,
                artifactStore: this._toolResultArtifactStore,
            });
            this._toolResultCompactionState = budgeted.nextCompactionState(this._toolResultCompactionState);
            if (!hookResult && normalizedContent === content && budgeted.content === content) {
                return undefined;
            }
            return {
                content: budgeted.content,
                details: hookResult?.details,
                isError: hookResult?.isError ?? isError,
                usage: hookResult?.usage,
            };
        };
""",
    )
    replace_once(
        session,
        """    async _compactBeforeNextAssistantResponse(context) {
        const model = this.model;
        const settings = this.settingsManager.getCompactionSettings();
        if (!model ||
            model.contextWindow <= 0 ||
            !shouldCompact(estimateContextTokens(context.messages).tokens, model.contextWindow, settings)) {
            return context;
        }
        await this._runAutoCompaction("threshold", false);
        return {
""",
        """    async _compactBeforeNextAssistantResponse(context) {
        const model = this.model;
        const settings = this.settingsManager.getCompactionSettings();
        const contextTokens = estimateContextTokens(context.messages).tokens;
        if (!model ||
            model.contextWindow <= 0 ||
            !this._toolResultCompactionState.shouldCompact(contextTokens, model.contextWindow, settings, shouldCompact)) {
            return context;
        }
        const previousCompaction = this.sessionManager.entryStore.latest(
            this.sessionManager.getLeafId(), "compaction")?.id;
        await this._runAutoCompaction("threshold", false);
        const currentCompaction = this.sessionManager.entryStore.latest(
            this.sessionManager.getLeafId(), "compaction")?.id;
        if (currentCompaction === previousCompaction) {
            throw new Error("The next model request requires context compaction, but no compaction could be prepared");
        }
        this._toolResultCompactionState = ToolResultCompactionState.shared;
        return {
""",
    )


if __name__ == "__main__":
    main(sys.argv[1])
