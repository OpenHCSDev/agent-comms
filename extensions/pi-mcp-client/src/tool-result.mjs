const IMAGE_MIME = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const MAX_TEXT = 32_000;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

/** Convert an MCP result to bounded Pi text/images without leaking raw binary/audio blobs. */
export function toPiResult(result) {
  const content = [];
  let textRemaining = MAX_TEXT;
  let truncated = false;
  const kinds = [];
  function text(value) {
    if (textRemaining <= 0) { truncated = true; return; }
    const chunk = value.slice(0, textRemaining);
    content.push({ type: 'text', text: chunk });
    textRemaining -= chunk.length;
    if (chunk.length !== value.length) truncated = true;
  }
  if (result.isError) text('MCP server reported a tool error.\n');
  const blocks = Array.isArray(result.content) ? result.content : [];
  if (blocks.length > 64) truncated = true;
  for (const item of blocks.slice(0, 64)) {
    kinds.push(item.type);
    if (item.type === 'text' && typeof item.text === 'string') {
      text(item.text);
    } else if (item.type === 'image' && IMAGE_MIME.has(item.mimeType) &&
      typeof item.data === 'string' && item.data.length <= Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 4 &&
      /^[A-Za-z0-9+/]*={0,2}$/.test(item.data)) {
      const bytes = Buffer.from(item.data, 'base64');
      if (bytes.length <= MAX_IMAGE_BYTES && bytes.toString('base64') === item.data) {
        content.push({ type: 'image', data: item.data, mimeType: item.mimeType });
      } else { text('[MCP image omitted: invalid or oversized data]'); truncated = true; }
    } else if (item.type === 'resource' && typeof item.resource?.text === 'string') {
      text(`[MCP resource ${item.resource.uri ?? ''}]\n${item.resource.text}`);
    } else {
      text(`[Unsupported MCP content kind: ${item.type ?? 'unknown'}]`);
      truncated = true;
    }
  }
  if (truncated) content.push({ type: 'text', text: '[MCP result truncated or unsupported content omitted]' });
  if (!content.length) content.push({ type: 'text', text: '(empty MCP result)' });
  return { content, details: { mcpIsError: !!result.isError, truncated,
    contentKinds: kinds.slice(0, 64), structuredContentPresent: result.structuredContent !== undefined } };
}
