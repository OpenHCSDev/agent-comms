// Opt-in, provider-free negative probe for pinned native SessionManager only.
// This is NOT adaptive runtime wiring, a provider transport, or an owner proof.
import { createInterface } from 'node:readline';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [mode, target] = process.argv.slice(2);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
if (!packageDir || !['pending', 'external-append'].includes(mode) || !target)
  throw new Error('Provide PI_NATIVE_PACKAGE_DIR and pending <root> or external-append <session-file>');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const message = text => ({ role: 'user', content: [{ type: 'text', text }], timestamp: Date.now() });

if (mode === 'external-append') {
  const writer = SessionManager.open(target);
  const id = writer.appendMessage(message('independent writer after preflight'));
  console.log(JSON.stringify({ appendedId: id, leaf: writer.getLeafId() }));
} else {
  const manager = SessionManager.create(target, join(target, 'sessions'));
  const firstKeptEntryId = manager.appendMessage(message('original task'));
  manager.appendMessage({
    role: 'assistant', content: [{ type: 'text', text: 'first answer' }],
    provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop',
    timestamp: Date.now(),
  });
  const capturedLeaf = manager.getLeafId();
  const capturedBranch = manager.getBranch();
  const sessionFile = manager.getSessionFile();
  if (!sessionFile) throw new Error('A persisted native session file is required');
  console.log(JSON.stringify({ phase: 'captured', capturedLeaf, sessionFile,
    branchLength: capturedBranch.length, firstKeptEntryId }));
  const readline = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const [action] = await (async () => { for await (const line of readline) return [line]; return []; })();
  readline.close();
  if (action === 'append-local') manager.appendMessage(message('later local input'));
  else if (action !== 'continue') throw new Error('Unexpected barrier action');
  const leafBeforeCommit = manager.getLeafId();
  let commitId = null;
  let commitError = null;
  try {
    // The API accepts no expected leaf/file/owner/goal token; this call must
    // eventually be guarded by the actual session writer, not this probe.
    commitId = manager.appendCompaction('STALE CANDIDATE', firstKeptEntryId, 42);
  } catch (error) {
    commitError = String(error);
  }
  console.log(JSON.stringify({ phase: 'committed', capturedLeaf, leafBeforeCommit,
    leafAfterCommit: manager.getLeafId(), commitId, commitError, sessionFile,
    nativeBranchTail: manager.getBranch().at(-1)?.type }));
}
