import { Type } from 'typebox';
import { confirmedClient } from './operation.mjs';
import { toPiResult } from './tool-result.mjs';

const limits = { timeout: 60_000, resetTimeoutOnProgress: true, maxTotalTimeout: 900_000 };

/** Pi has no native MCP resource/prompt registry: expose a bounded index and operations. */
export function registerResourceTools(pi, runtime) {
  const ready = runtime.snapshot().filter((entry) => entry.status === 'ready');
  if (!ready.length) return;
  const names = ['mcp_catalog', 'mcp_read_resource', 'mcp_get_prompt'];
  const existing = new Set(pi.getAllTools().map((tool) => tool.name));
  if (names.some((name) => existing.has(name))) throw new Error('MCP resource/prompt tool name collision');
  pi.registerTool({
    name: 'mcp_catalog', label: 'MCP resource and prompt catalog',
    description: 'List names, URIs and descriptions of discovered MCP resources and prompts from connected servers.',
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
      const entries = [];
      for (const { id, status } of runtime.snapshot()) {
        if (status !== 'ready' || !await runtime.authorized(id, ctx)) continue;
        const catalog = runtime.ready(id)?.catalog;
        entries.push({ serverId: id,
          resources: catalog?.resources.map(({ name, uri, description }) => ({
            name, uri, description: String(description ?? '').slice(0, 256) })) ?? [],
          prompts: catalog?.prompts.map(({ name, description, arguments: args }) => ({
            name, description: String(description ?? '').slice(0, 256), arguments: args ?? [] })) ?? [],
        });
      }
      return toPiResult({ content: [{ type: 'text', text: JSON.stringify(entries) }] });
    },
  });
  pi.registerTool({
    name: 'mcp_read_resource', label: 'Read an MCP resource',
    description: 'Read a discovered MCP resource URI from one connected server, with local human confirmation.',
    parameters: Type.Object({ serverId: Type.String(), uri: Type.String() }),
    async execute(_toolCallId, { serverId, uri }, signal, _onUpdate, ctx) {
      const catalog = runtime.ready(serverId)?.catalog;
      if (!catalog?.resources.some((resource) => resource.uri === uri)) {
        throw new Error('MCP resource URI was not discovered');
      }
      const client = await confirmedClient(runtime, serverId, 'resources/read', { uri }, ctx, signal);
      const result = await client.readResource({ uri }, { ...limits, signal });
      const mapped = toPiResult({ content: result.contents.map((resource) => ({ type: 'resource', resource })) });
      mapped.details.serverId = serverId;
      mapped.details.resourceUri = uri;
      return mapped;
    },
  });
  pi.registerTool({
    name: 'mcp_get_prompt', label: 'Get an MCP prompt',
    description: 'Fetch a discovered MCP prompt from one connected server, with local human confirmation.',
    parameters: Type.Object({ serverId: Type.String(), name: Type.String(),
      arguments: Type.Optional(Type.Record(Type.String(), Type.String())) }),
    async execute(_toolCallId, { serverId, name, arguments: args }, signal, _onUpdate, ctx) {
      const catalog = runtime.ready(serverId)?.catalog;
      if (!catalog?.prompts.some((prompt) => prompt.name === name)) {
        throw new Error('MCP prompt name was not discovered');
      }
      const client = await confirmedClient(runtime, serverId, 'prompts/get', { name, arguments: args }, ctx, signal);
      const result = await client.getPrompt({ name, arguments: args ?? {} }, { ...limits, signal });
      const content = result.messages.flatMap(({ role, content: block }) =>
        block.type === 'text' ? [{ type: 'text', text: `[MCP prompt ${role}]\n${block.text}` }]
          : [{ type: 'text', text: `[MCP prompt ${role}]` }, block]);
      const mapped = toPiResult({ content });
      mapped.details.serverId = serverId;
      mapped.details.promptName = name;
      return mapped;
    },
  });
}
