// Negative executable contract for stock Pi 0.85.1. These tests intentionally FAIL
// until native input identity and a durable post-transformation context hook exist.
// Run only in a disposable environment; no model/API/network access is required.
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

const root = process.env.PI_PACKAGE_DIR ?? '/home/ts/.local/pi-npm/lib/node_modules/@earendil-works/pi-coding-agent';
const source = (path) => readFileSync(join(root, 'dist', path), 'utf8');

// The RPC command id is an ACK correlation id and cannot substitute for inputId:
// ACK may precede actual queue delivery and the command may be transformed.
test('RPC carries an independent inputId into AgentSession.prompt', () => {
  const rpc = source('modes/rpc/rpc-mode.js');
  const promptCase = rpc.slice(rpc.indexOf('case "prompt":'), rpc.indexOf('case "steer":'));
  assert.match(promptCase, /inputId:\s*command\.inputId\b/);
});

test('AgentSession preserves inputId through idle, steering and followUp user messages', () => {
  const session = source('core/agent-session.js');
  const prompt = session.slice(session.indexOf('async prompt(text, options)'), session.indexOf('async _tryExecuteExtensionCommand'));
  assert.match(prompt, /inputId:\s*options\?\.inputId\b/);
  assert.match(prompt, /_queueSteer\(expandedText, currentImages, options\?\.inputId\)/);
  assert.match(prompt, /_queueFollowUp\(expandedText, currentImages, options\?\.inputId\)/);
  const queueStart = session.indexOf('async _queueSteer(');
  const queues = session.slice(queueStart, session.indexOf('_throwIfExtensionCommand(', queueStart));
  assert.match(queues, /inputId\b/);
});

test('context-ready proof occurs after transforms and LLM conversion, before provider call', () => {
  const loop = readFileSync(join(root, 'node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js'), 'utf8');
  const transform = loop.indexOf('messages = await config.transformContext(messages, signal)');
  const converted = loop.indexOf('const llmMessages = await config.convertToLlm(messages)');
  const ready = loop.indexOf('await config.onContextReady?.(llmContext');
  const provider = loop.indexOf('await streamFunction(config.model, llmContext');
  assert(transform >= 0 && converted > transform && ready > converted && provider > ready);
});

test('first user input and inputId have an explicit fsync durability boundary', async () => {
  const sessionManager = await import(pathToFileURL(join(root, 'dist/core/session-manager.js')).href);
  const cwd = mkdtempSync(join(tmpdir(), 'pi-input-id-negative-'));
  try {
    const sm = sessionManager.SessionManager.create(cwd, join(cwd, 'sessions'));
    sm.appendMessage({
      role: 'user',
      content: [{ type: 'text', text: 'identical duplicate' }],
      inputId: '0123456789abcdef0123456789abcdef',
      timestamp: Date.now(),
    });
    // Current Pi has no file yet. A tracked-input-only explicit flush must
    // create one before provider invocation without changing ordinary saves.
    assert.equal(existsSync(sm.getSessionFile()), false);
    assert.equal(typeof sm.flushInputDurably, 'function');
    sm.flushInputDurably();
    assert(existsSync(sm.getSessionFile()));
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});
