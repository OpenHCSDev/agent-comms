#!/usr/bin/env python3
"""Explicit interruption for already queued native inputs; never retry the old input."""

import hashlib
import sys
from pathlib import Path

SESSION_SHA = "86dd10ff53999485ea184873ac1b5a32126898d1fb32865db884aadb041f4e09"
RPC_SHA = "6f5438c028032f3bc212b6270a1acf9d3e5d22ce5837b85ff5a6fea6c240c76a"
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
        const selected = interruptedIds ? this.agent.steeringQueue.messages.filter(message =>
            interruptedIds.includes(message.inputId)) : [];
        if (selected.length) {
            this.agent.steeringQueue.messages = this.agent.steeringQueue.messages.filter(message =>
                !interruptedIds.includes(message.inputId));
            this._nativeRunHadTrackedInput = true;
            this._emit({ type: "steering_interrupt_completed" });
            return selected;
        }
        // Read the actual completed run's signal, including cancellation during
        // awaited listeners. Plain abort never authorizes an implicit continuation.
        if (this._lastAgentRunSignal?.aborted) return false;
"""


def replace_once(source, anchor, replacement):
    if source.count(anchor) != 1:
        raise SystemExit(f"Native steering anchor changed: {anchor!r}")
    return source.replace(anchor, replacement, 1)


def main(package):
    session = package / "dist/core/agent-session.js"
    rpc = package / "dist/modes/rpc/rpc-mode.js"
    loop = package / "node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js"
    for path, expected in ((session, SESSION_SHA), (rpc, RPC_SHA), (loop, LOOP_SHA)):
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
    session.write_text(source)
    source = replace_once(
        rpc.read_text(),
        '            case "steer": {\n',
        '            case "interrupt_steering": {\n'
        '                return success(id, "interrupt_steering", '
        "{ interrupted: session.interruptSteering(command.inputIds) });\n"
        '            }\n            case "steer": {\n',
    )
    rpc.write_text(source)
    loop.write_text(
        replace_once(
            loop.read_text(),
            "            lastCompletedTurn = {\n",
            ABORT_AFTER_TOOLS + "            lastCompletedTurn = {\n",
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
