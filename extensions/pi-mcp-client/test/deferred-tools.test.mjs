import assert from 'node:assert/strict';
import { test } from 'node:test';
import { registerReadyTools } from '../src/tools.mjs';
import { ToolRegistry } from './tool-registry.mjs';

function catalog(size) {
  return { tools: Array.from({ length: size }, (_, index) => ({
    name: `tool_${index}`, description: `Fixture tool ${index}`,
    inputSchema: { type: 'object', properties: { message: { type: 'string' } }, required: ['message'] },
  })) };
}

test('125 tools retain every schema; lookup selects any tool with bounded active exposure and unchanged approval', async () => {
  const definitions = catalog(125);
  definitions.tools[0].name = 'x'.repeat(128);
  let calls = 0;
  const runtime = {
    snapshot: () => [{ id: 'fixture', scope: 'user', status: 'ready' }],
    ready: () => ({ catalog: definitions, client: { async callTool({ name, arguments: args }) {
      calls++;
      return { content: [{ type: 'text', text: `${name}:${args.message}` }] };
    } } }),
    authorized: async () => true,
    preauthorized: async () => false,
  };
  const pi = new ToolRegistry([{ name: 'read' }]);
  const records = registerReadyTools(pi, runtime);
  assert.equal(records.length, definitions.tools.length);
  assert.equal(pi.getAllTools().length, definitions.tools.length + 2);
  assert.deepEqual(pi.getActiveTools(), ['read', 'mcp_find_tools']);
  for (const [index, record] of records.entries()) {
    assert.deepEqual(pi.tool(record.name).parameters, definitions.tools[index].inputSchema);
  }
  const picker = pi.tool('mcp_find_tools');
  const broad = await picker.execute('broad', { query: 'fixture' });
  assert.equal(broad.details.matching, definitions.tools.length);
  assert.equal(broad.details.active, 64);
  assert.equal(pi.getActiveTools().length, 66);
  assert.equal(broad.content[0].text.includes(definitions.tools[0].name), false);
  const firstPage = JSON.parse(broad.content[0].text);
  const next = await picker.execute('next', { query: 'fixture', offset: firstPage.nextOffset });
  const lastPage = JSON.parse(next.content[0].text);
  assert.equal(lastPage.nextOffset, null);
  assert.deepEqual(new Set([...firstPage.active, ...lastPage.active]), new Set(records.map(({ name }) => name)));
  const last = records.at(-1);
  await picker.execute('last', { query: last.name });
  assert.deepEqual(pi.getActiveTools(), ['read', 'mcp_find_tools', last.name]);
  assert.equal(pi.getAllTools().length, definitions.tools.length + 2);
  assert.equal(calls, 0);
  const signal = new AbortController().signal;
  const ctx = { mode: 'rpc', isProjectTrusted: () => false, ui: { confirm: async () => false } };
  await assert.rejects(pi.tool(last.name).execute('deny', { message: 'ok' }, signal, undefined, ctx), /denied by user/);
  assert.equal(calls, 0);
  const result = await pi.tool(last.name).execute('allow', { message: 'ok' }, signal, undefined,
    { ...ctx, ui: { confirm: async () => true } });
  assert.equal(result.content[0].text, 'tool_124:ok');
  assert.equal(calls, 1);
});

test('large catalogs still reject duplicate identities and picker collisions before partial registration', () => {
  const duplicate = catalog(125);
  duplicate.tools[124] = duplicate.tools[0];
  const runtime = { snapshot: () => [{ id: 'fixture', status: 'ready' }],
    ready: () => ({ catalog: duplicate }) };
  const pi = new ToolRegistry();
  assert.throws(() => registerReadyTools(pi, runtime), /duplicate identities/);
  assert.equal(pi.getAllTools().length, 0);
  const collision = new ToolRegistry([{ name: 'mcp_find_tools' }]);
  assert.throws(() => registerReadyTools(collision, { ...runtime, ready: () => ({ catalog: catalog(125) }) }),
    /name collision/);
  assert.equal(collision.getAllTools().length, 1);
});
