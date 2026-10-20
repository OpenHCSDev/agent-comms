// A controlled request to the installed native edit tool; persist its actual result.
import { pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';
const [packagePath, sessionPath, path, callId, replacement] = process.argv.slice(2);
const sdk = pathToFileURL(`${packagePath}/dist/index.js`).href;
const { createEditTool, SessionManager } = await import(sdk);
const args = {path, edits: [{oldText: await readFile(path, 'utf8'), newText: replacement}]};
const session = SessionManager.open(sessionPath);
session.appendMessage({role: 'user', content: 'Execute one isolated verdict-check edit.',
  timestamp: Date.now()});
session.appendMessage({
  role: 'assistant', content: [{type: 'toolCall', id: callId, name: 'edit', arguments: args}],
  api: 'controlled-verification', provider: 'controlled-verification', model: 'local-native-edit',
  usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}},
  stopReason: 'toolUse', timestamp: Date.now(),
});
const result = await createEditTool(process.cwd()).execute(callId, args);
if ((await readFile(path, 'utf8')) !== replacement) {
  throw new Error('Native edit did not apply its request');
}
session.appendMessage({role: 'toolResult', toolCallId: callId, toolName: 'edit',
  content: result.content, details: result.details, isError: false, timestamp: Date.now()});
