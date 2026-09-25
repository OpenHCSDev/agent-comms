// Offline proof that a tracked next prompt compacts a successful near-full session first.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const manifest = readFileSync(resolve(import.meta.dirname, 'pi-native.sha256'));
const buildId = createHash('sha256').update(manifest).digest('hex').slice(0, 16);
const packageDir = process.env.PI_NATIVE_PACKAGE_DIR ?? resolve(
  import.meta.dirname, `.pi-native-${buildId}/node_modules/@earendil-works/pi-coding-agent`,
);
const pi = await import(pathToFileURL(join(packageDir, 'dist/index.js')).href);
const root = mkdtempSync(join(tmpdir(), 'agent-comms-auto-compact-'));
const id = (character) => character.repeat(32);
const usage = {
  input: 115_977, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 115_978,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

try {
  const runtime = await pi.ModelRuntime.create({
    authPath: join(root, 'auth.json'), modelsPath: join(root, 'models.json'),
    modelsStorePath: join(root, 'models-store.json'),
  });
  await runtime.setRuntimeApiKey('openai', 'offline-fixture');
  const model = { ...runtime.getModel('openai', 'gpt-4.1-mini'), contextWindow: 128_000 };
  const settings = pi.SettingsManager.inMemory({
    compaction: { enabled: true, reserveTokens: 16_384, keepRecentTokens: 20_000 },
    retry: { enabled: false },
  });
  const loader = new pi.DefaultResourceLoader({
    cwd: root, agentDir: root, settingsManager: settings,
  });
  await loader.reload();
  const { session } = await pi.createAgentSession({
    cwd: root, agentDir: root, modelRuntime: runtime, model,
    sessionManager: pi.SessionManager.create(root, join(root, 'sessions')),
    settingsManager: settings, resourceLoader: loader, noTools: 'all',
  });
  try {
    const order = [];
    let responses = 0;
    session.agent.streamFunction = async () => {
      order.push(`provider-${++responses}`);
      const message = {
        role: 'assistant', content: [{ type: 'text', text: 'OK' }],
        api: model.api, provider: model.provider, model: model.id,
        usage, stopReason: responses === 3 ? 'error' : 'stop', timestamp: Date.now(),
      };
      return {
        async *[Symbol.asyncIterator]() { yield { type: 'done', message }; },
        async result() { return message; },
      };
    };
    session._runAutoCompaction = async (reason, willRetry) => {
      order.push(`compact-${reason}-${willRetry}`);
      return false;
    };
    await session.prompt('first', { inputId: id('a') });
    assert.deepEqual(order, ['provider-1']);
    await session.prompt('second', { inputId: id('b') });
    assert.deepEqual(order, ['provider-1', 'compact-threshold-false', 'provider-2']);
    await session.prompt('third', { inputId: id('c') });
    assert.deepEqual(order, [
      'provider-1', 'compact-threshold-false', 'provider-2',
      'compact-threshold-false', 'provider-3',
    ]);
    await session.prompt('fourth', { inputId: id('d') });
    assert.deepEqual(order, [
      'provider-1', 'compact-threshold-false', 'provider-2',
      'compact-threshold-false', 'provider-3', 'provider-4',
    ], 'a failed tracked attempt must not trigger automatic compaction or replay');
  } finally {
    session.dispose();
  }
} finally {
  rmSync(root, { recursive: true, force: true });
}
