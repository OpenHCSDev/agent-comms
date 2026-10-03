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
    function = function.replace("    for await (const event of response) {",
                                "    for await (const event of request.events(response)) {", 1)
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
    replace_once(agent, "    onResponse;", "    onResponse;\n    onRequestProgress;")
    replace_once(agent, "        this.onResponse = runtimeOptions.onResponse;",
                 "        this.onResponse = runtimeOptions.onResponse;\n        this.onRequestProgress = runtimeOptions.onRequestProgress;")
    replace_once(agent, "            onContextReady: this.onContextReady,",
                 "            onContextReady: this.onContextReady,\n            onRequestProgress: this.onRequestProgress,")
    session = package / "dist/core/agent-session.js"
    replace_once(session, "        this.agent.onContextReady = ",
                 """        this.agent.onRequestProgress = progress => this._emitNativePresentation({
            type: "model_request_progress", progress });
        this.agent.onContextReady = """)
    replace_once(session, """    _summarizationRetryCallbacks(source) {
        return {""", """    _summarizationRetryCallbacks(source) {
        return {
            onRequestProgress: this.agent.onRequestProgress,""")
    summary = package / "dist/core/compaction/compaction.js"
    summary.write_text(
        'import { NativeRequestObservation } from "../../../node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js";\n'
        + summary.read_text()
    )
    replace_once(summary, """    const produce = async () => {
        const stream = await (streamFn ?? streamSimple)(model, context, requestOptions);
        for await (const event of stream) {""", """    const request = new NativeRequestObservation({ ...requestOptions,
        onRequestProgress: callbacks?.onRequestProgress }, context);
    request.observe({ stage: "preparing", detail: "Preparing compaction request" });
    try {
    const produce = async () => {
        const stream = await (streamFn ?? streamSimple)(model, context, request.options(requestOptions));
        for await (const event of request.events(stream)) {""")
    replace_once(summary, """    if (response.stopReason === 'stop' && !contentText(response.content).trim()) throw new Error('Compaction returned an empty summary');
    return response;
}""", """    if (response.stopReason === 'stop' && !contentText(response.content).trim()) throw new Error('Compaction returned an empty summary');
    return response;
    } finally { request.observe({ stage: "finished", detail: "Compaction request finished" }); }
}""")
    helper = package / "node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js"
    helper.write_bytes(Path(__file__).with_name("native-request-observation.mjs").read_bytes())
    helper.with_suffix(".d.ts").write_bytes(Path(__file__).with_name("native-request-observation.d.ts").read_bytes())
    replace_once(helper.with_name("retry.d.ts"), "export interface RetryCallbacks {",
                 'export interface RetryCallbacks {\n    onRequestProgress?: (progress: import("./agent-comms-request-observation.js").NativeRequestProgress) => void;')
    retry = helper.with_name("provider-retry.js")
    replace_once(retry, "const DEFAULT_MAX_RETRY_DELAY_MS",
                 'import { observeRequest } from "./agent-comms-request-observation.js";\nconst DEFAULT_MAX_RETRY_DELAY_MS')
    replace_once(retry, "            return await request();",
                 "            return await request(maxRetries - retriesRemaining);")
    replace_once(retry,
        "            await abortableSleep(getRetryDelayMs(error, retryIndex, options.maxRetryDelayMs), options.signal);",
        """            const delayMs = getRetryDelayMs(error, retryIndex, options.maxRetryDelayMs);
            observeRequest(options, { stage: "retry", attempt: retryIndex + 1,
                status: error.status, detail: `Provider retry waits ${Math.ceil(delayMs / 1000)}s` });
            await abortableSleep(delayMs, options.signal);""")
    replace_once(retry.with_suffix(".d.ts"), "request: () => Promise<T>",
                 "request: (attempt: number) => Promise<T>")
    replace_once(retry.with_suffix(".d.ts"), "    signal?: AbortSignal;",
                 '    signal?: AbortSignal;\n    onRequestProgress?: import("./agent-comms-request-observation.js").RequestObserver;')
    declaration = '    onRequestProgress?: (progress: import("../../pi-ai/dist/utils/agent-comms-request-observation.js").NativeRequestProgress) => void;\n'
    for name in ("agent.d.ts", "types.d.ts"):
        path = core / name
        source = path.read_text()
        anchor = '    onContextReady?: (assembledContext: import("@earendil-works/pi-ai").Context) => Promise<void>;\n'
        if anchor not in source:
            raise ValueError(f"Native context declaration changed: {name}")
        path.write_text(source.replace(anchor, anchor + declaration))
    path = package / "dist/core/agent-session.d.ts"
    replace_once(path, '    type: "context_committed";',
        '    type: "model_request_progress";\n    progress: import("../../node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js").NativeRequestProgress;\n} | {\n    type: "context_committed";')


if __name__ == "__main__":
    main(Path(sys.argv[1]))
