// A controlled request to the installed native edit tool; persist its actual result.
// No provider call, replacement tool, fabricated result, or original session write.
import { pathToFileURL } from "node:url";
const [packagePath, sessionPath, path, callId, replacement] = process.argv.slice(2);
const { createEditTool, SessionManager } = await import(pathToFileURL(`${packagePath}/dist/index.js`).href);
const { readFile } = await import("node:fs/promises");
const oldText = await readFile(path, "utf8");
const args = { path, edits: [{ oldText, newText: replacement }] };
const session = SessionManager.open(sessionPath);
session.appendMessage({ role: "user", content: "Execute one isolated verdict-check edit.", timestamp: Date.now() });
session.appendMessage({
  role: "assistant", content: [{ type: "toolCall", id: callId, name: "edit", arguments: args }],
  api: "controlled-verification", provider: "controlled-verification", model: "local-native-edit",
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
  stopReason: "toolUse", timestamp: Date.now(),
});
const result = await createEditTool(process.cwd()).execute(callId, args);
session.appendMessage({ role: "toolResult", toolCallId: callId, toolName: "edit",
  content: result.content, details: result.details, isError: false, timestamp: Date.now() });
console.log(JSON.stringify({ callId, changed: (await readFile(path, "utf8")) === replacement }));
