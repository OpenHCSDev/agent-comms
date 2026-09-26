// Opt-in, provider-free smoke for the disposable PR48 native writer prototype.
// Not invoked by prepare-pi-native or normal runtime; requires explicit copied package.
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const packageDir = process.env.PI_NATIVE_PACKAGE_DIR;
if (!packageDir) throw new Error('Explicit disposable PI_NATIVE_PACKAGE_DIR required');
const { SessionManager } = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const root = mkdtempSync(join(tmpdir(), 'pr48-native-cas-smoke-'));
const user = text => ({ role: 'user', content: [{ type: 'text', text }], timestamp: 1 });
const assistant = text => ({ role: 'assistant', content: [{ type: 'text', text }],
  provider: 'fixture', model: 'fixture', api: 'fixture', stopReason: 'stop', timestamp: 2 });
try {
  const manager = SessionManager.create(root, join(root, 'sessions'));
  const first = manager.appendMessage(user('original'));
  manager.appendMessage(assistant('answer'));
  const originalFile = manager.getSessionFile();
  const originalLeaf = manager.getLeafId();
  const branchFile = manager.createBranchedSession(originalLeaf);
  assert.ok(branchFile && branchFile !== originalFile);
  manager.appendMessage(user('branch follow-up'));
  assert.equal(SessionManager.open(branchFile).getLeafId(), manager.getLeafId());
  manager.newSession();
  manager.appendMessage(user('new session'));
  manager.appendMessage(assistant('new answer'));
  assert.equal(SessionManager.open(manager.getSessionFile()).getLeafId(), manager.getLeafId());
  manager.setSessionFile(originalFile);
  assert.equal(manager.getLeafId(), originalLeaf);
  const witness = manager.captureCompactionWitness(first);
  const committed = manager.appendCompactionIfCurrent(witness, 'summary', 42);
  assert.equal(SessionManager.open(originalFile).getLeafId(), committed);
  console.log('native writer prototype branch/new/resume/positive PASS');
} finally {
  rmSync(root, { recursive: true, force: true });
}
