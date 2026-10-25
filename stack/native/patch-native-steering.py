#!/usr/bin/env python3
"""Explicit interruption for already queued native inputs; never retry the old input."""

import hashlib
import sys
from pathlib import Path

SESSION_SHA = "55476990885e28aaf6137a8df914494095bcdb4a34fd9ac732fd134c3c9ada27"
AGENT_SHA = "93ed16306399765e79c11f78897f575252d7174b85b81a01cdebb2a479e0e57f"
RPC_SHA = "d8eecb3d5bbffd5c516dc14a3675b39b227a6eda5cf1e506945dab3d76107927"
LOOP_SHA = "7469aebae3badc125087c55843130fe44c2ea3b68ea1b757c884469dd355d5f4"
ABORT_AFTER_TOOLS = """\
            // Aborting a tool ends this run before queued inputs can be consumed.
            // Explicit steering resumes those inputs with a fresh AbortController;
            // ordinary cancellation leaves them unstarted rather than replaying them.
            if (signal?.aborted) {
                await emit({ type: "agent_end", messages: newMessages });
                return;
            }
"""
METHOD = """    interruptSteering(inputIds) {
        if (!Array.isArray(inputIds) || inputIds.some(id => !/^[0-9a-f]{32}$/.test(id)))
            throw new Error("Invalid native steering input IDs");
        if (this.isCompacting) throw new Error("Wait for compaction before Send now");
        if (!this.isStreaming || this._nativeInterruptIds) return false;
        const pending = this.agent.steeringQueue.messages.filter(message =>
            inputIds.includes(message.inputId) && this._nativeInputClaims.has(message.inputId));
        if (!pending.length) return false;
        this._nativeInterruptIds = pending.map(message => message.inputId);
        this._emit({ type: "steering_interrupt_started" });
        this.agent.abort();
        return true;
    }
"""
CONTINUE = """        // Only a new explicit Send now command permits this continuation.
        // Consume the existing queued native input, never replay the interrupted input.
        const interruptedIds = this._nativeInterruptIds;
        this._nativeInterruptIds = undefined;
        const selected = interruptedIds
            ? this.agent.steeringQueue.remove(new Set(interruptedIds)) : [];
        if (selected.length) {
            this._nativeRunHadTrackedInput = true;
            this._emit({ type: "steering_interrupt_completed" });
            return selected;
        }
        // Read the actual completed run's signal, including cancellation during
        // awaited listeners. Plain abort never authorizes an implicit continuation.
        if (this._lastAgentRunSignal?.aborted) return false;
"""


REMOVE = """    remove(inputIds) {
        const removed = [], remaining = [];
        for (const message of this.messages) {
            (inputIds.has(message.inputId) ? removed : remaining).push(message);
        }
        this.messages = remaining;
        return removed;
    }
"""
CLEAR_QUEUE_OLD = """    clearQueue() {
        const steering = this._steeringMessages.map((item) => item.text);
        const followUp = this._followUpMessages.map((item) => item.text);
        const removed = this.agent.clearAllQueues();
        const removedIds = new Set([...removed.steering, ...removed.followUp]
            .map((message) => message.inputId).filter(Boolean));
        for (const item of [...this._steeringMessages, ...this._followUpMessages]) {
            if (item.inputId && removedIds.has(item.inputId) &&
                !this.sessionManager.getTrackedInput(item.inputId)) {
                this._nativeInputClaims.delete(item.inputId);
            }
        }
        this._steeringMessages = [];
        this._followUpMessages = [];
        this._emitQueueUpdate();
        return { steering, followUp };
    }
"""
CLEAR_QUEUE = """    clearQueue(inputIds) {
        // Direct interactive callers still clear their whole editor queue.
        // The Core RPC boundary below requires an explicit native-ID selection.
        if (inputIds !== undefined && (!Array.isArray(inputIds) ||
            inputIds.some(id => !/^[0-9a-f]{32}$/.test(id))))
            throw new Error("Invalid native queue input IDs");
        const clearAll = inputIds === undefined;
        const removed = clearAll ? this.agent.clearAllQueues()
            : this.agent.clearQueuedInputs(inputIds);
        const removedIds = new Set([...removed.steering, ...removed.followUp]
            .map(message => message.inputId).filter(Boolean));
        const wasRemoved = item => clearAll || removedIds.has(item.inputId);
        const steering = this._steeringMessages.filter(wasRemoved).map(item => item.text);
        const followUp = this._followUpMessages.filter(wasRemoved).map(item => item.text);
        for (const inputId of removedIds) {
            if (!this.sessionManager.getTrackedInput(inputId))
                this._nativeInputClaims.delete(inputId);
        }
        this._steeringMessages = this._steeringMessages.filter(item => !wasRemoved(item));
        this._followUpMessages = this._followUpMessages.filter(item => !wasRemoved(item));
        this._emitQueueUpdate();
        return { steering, followUp, inputIds: [...removedIds] };
    }
"""


def replace_once(source, anchor, replacement):
    if source.count(anchor) != 1:
        raise SystemExit(f"Native steering anchor changed: {anchor!r}")
    return source.replace(anchor, replacement, 1)


def main(package):
    session = package / "dist/core/agent-session.js"
    rpc = package / "dist/modes/rpc/rpc-mode.js"
    core = package / "node_modules/@earendil-works/pi-agent-core/dist"
    loop = core / "agent-loop.js"
    agent = core / "agent.js"
    for path, expected in ((session, SESSION_SHA), (rpc, RPC_SHA), (loop, LOOP_SHA), (agent, AGENT_SHA)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit("Native steering source does not match pinned build")
    source = session.read_text()
    source = replace_once(
        source,
        "    async _handlePostAgentRun() {\n",
        METHOD + "    async _handlePostAgentRun() {\n" + CONTINUE,
    )
    source = replace_once(
        source,
        "    async abort() {\n",
        "    async abort() {\n        this._nativeInterruptIds = undefined;\n",
    )
    source = replace_once(
        source,
        "            while (await this._handlePostAgentRun()) {\n"
        "                await this.agent.continue();\n"
        "            }",
        "            let continuation;\n"
        "            while ((continuation = await this._handlePostAgentRun())) {\n"
        "                if (Array.isArray(continuation))\n"
        "                    await this.agent.runPromptMessages(continuation, "
        "{ skipInitialSteeringPoll: true });\n"
        "                else await this.agent.continue();\n"
        "            }",
    )
    source = replace_once(
        source,
        "    _handleAgentEvent = async (event) => {\n",
        "    _handleAgentEvent = async (event, signal) => {\n"
        '        if (event.type === "agent_end") this._lastAgentRunSignal = signal;\n',
    )
    session.write_text(replace_once(source, CLEAR_QUEUE_OLD, CLEAR_QUEUE))
    source = replace_once(
        rpc.read_text(),
        '            case "steer": {\n',
        '            case "interrupt_steering": {\n'
        '                return success(id, "interrupt_steering", '
        "{ interrupted: session.interruptSteering(command.inputIds) });\n"
        '            }\n            case "steer": {\n',
    )
    source = replace_once(
        source,
        '                return success(id, "clear_queue", session.clearQueue());',
        '                if (command.inputIds === undefined)\n'
        '                    throw new Error("clear_queue requires inputIds");\n'
        '                return success(id, "clear_queue", session.clearQueue(command.inputIds));',
    )
    rpc.write_text(source)
    source = replace_once(agent.read_text(), "    clear() {\n", REMOVE + "    clear() {\n")
    source = replace_once(
        source, "    clearAllQueues() {\n",
        "    clearQueuedInputs(inputIds) {\n"
        "        const selected = new Set(inputIds);\n"
        "        return { steering: this.steeringQueue.remove(selected),\n"
        "            followUp: this.followUpQueue.remove(selected) };\n"
        "    }\n    clearAllQueues() {\n",
    )
    agent.write_text(source)
    client = package / "dist/modes/rpc/rpc-client.js"
    client.write_text(replace_once(
        client.read_text(),
        '    async clearQueue() {\n        const response = await this.send({ type: "clear_queue" });',
        '    async clearQueue(inputIds) {\n'
        '        const response = await this.send({ type: "clear_queue", inputIds });',
    ))
    for relative, signature in (("dist/core/agent-session.d.ts", "clearQueue(inputIds?: string[])"),
                                ("dist/modes/rpc/rpc-client.d.ts", "clearQueue(inputIds: string[])")):
        declaration = package / relative
        source = replace_once(declaration.read_text(), "clearQueue()", signature)
        source = replace_once(source, "        followUp: string[];\n", "        followUp: string[];\n        inputIds: string[];\n")
        declaration.write_text(source)
    declaration = package / "dist/modes/rpc/rpc-types.d.ts"
    source = replace_once(declaration.read_text(), '    type: "clear_queue";',
                          '    type: "clear_queue";\n    inputIds: string[];')
    source = replace_once(source,
                          '    command: "clear_queue";\n    success: true;\n    data: {\n',
                          '    command: "clear_queue";\n    success: true;\n    data: {\n        inputIds: string[];\n')
    declaration.write_text(source)
    declaration = core / "agent.d.ts"
    declaration.write_text(replace_once(declaration.read_text(), "    clearAllQueues(): void;",
        "    clearQueuedInputs(inputIds: string[]): { steering: AgentMessage[]; followUp: AgentMessage[] };\n"
        "    clearAllQueues(): void;"))
    loop.write_text(
        replace_once(
            loop.read_text(),
            "            lastCompletedTurn = {\n",
            ABORT_AFTER_TOOLS + "            lastCompletedTurn = {\n",
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
