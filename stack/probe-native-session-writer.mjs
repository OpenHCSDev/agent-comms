// Provider-free writer probe for the pinned native SessionManager: one process
// appends a short burst under the shared per-session writer lock.
import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [mode, target] = process.argv.slice(2);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
if (!packageDir || mode !== 'append-burst' || !target)
  throw new Error('Provide PI_NATIVE_PACKAGE_DIR and append-burst <session-file>');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const message = text => ({ role: 'user', content: [{ type: 'text', text }], timestamp: Date.now() });

// Many processes append concurrently; every append must either commit under
// the shared lock or refuse before mutation.
const gate = process.env.PR48_BURST_GATE;
const writer = SessionManager.open(target);
if (gate) {
  // Open the session first (records the loaded revision), report readiness,
  // then hold appends until the go gate aligns every writer's first append.
  writeFileSync(`${gate}.open-${process.pid}`, 'open');
  while (!existsSync(gate)) await new Promise(resolve => setTimeout(resolve, 1));
}
let appended = 0;
const refusals = [];
for (let index = 0; index < 3; index += 1) {
  // Any failed mutator retires this manager, including pre-write contention.
  // Never retry on the same object or silently construct a replacement here.
  try {
    writer.appendMessage(message(`burst ${process.pid} ${index}`));
    appended += 1;
  } catch (error) {
    refusals.push(String(error));
  }
  if (refusals.length > 0) break;
}
console.log(JSON.stringify({ pid: process.pid, appended, refusals }));
