#!/usr/bin/env python3
"""Attach one request timing resource to the existing native loop and observers."""
from pathlib import Path
import sys


def replace_once(path, old, new):
    source = path.read_text()
    if source.count(old) != 1:
        raise ValueError(f"Native request observation boundary changed: {path.name}")
    path.write_text(source.replace(old, new, 1))


def main(package):
    core = package / "node_modules/@earendil-works/pi-agent-core/dist"
    loop = core / "agent-loop.js"
    source = loop.read_text()
    start = source.index("async function streamAssistantResponse(")
    end = source.index("/**", start)
    function = source[start:end]
    function = function.replace("    // Apply context transform", """    const request = new NativeRequestObservation(config, context);
    const publish = event => request.emit(event, emit);
    request.observe({ stage: "preparing", detail: "Preparing model request" });
    try {
    // Apply context transform""", 1)
    function = function.replace("...config,\n        apiKey:", "...request.options(config),\n        apiKey:", 1)
    function = function.replace("await emit(", "await publish(")
    # Even a failed provider/callback closes the same acquired diagnostic scope.
    ending = "    return finalMessage;\n}\n"
    if function.count(ending) != 1:
        raise ValueError("Native stream return boundary changed")
    function = function.replace(ending, """    return finalMessage;
    } finally { request.observe({ stage: "finished", detail: "Model request finished" }); }
}
""")
    loop.write_text('import { NativeRequestObservation } from "../../pi-ai/dist/utils/agent-comms-request-observation.js";\n'
                    + source[:start] + function + source[end:])
    agent = core / "agent.js"
    replace_once(agent, "            onContextReady: this.onContextReady,",
                 "            onContextReady: this.onContextReady,\n            onRequestProgress: this.onRequestProgress,")
    session = package / "dist/core/agent-session.js"
    replace_once(session, "        this.agent.onContextReady = async (context) => await this._commitNativeContext(context);",
                 """        this.agent.onContextReady = async (context) => await this._commitNativeContext(context);
        this.agent.onRequestProgress = progress => this._emitNativePresentation({
            type: "model_request_progress", progress });""")
    helper = package / "node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js"
    helper.write_bytes(Path(__file__).with_name("native-request-observation.mjs").read_bytes())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
