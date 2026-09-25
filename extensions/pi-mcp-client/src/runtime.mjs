import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { callGrantDecision } from './authority.mjs';
import { discover } from './discover.mjs';
import { prepareStdioParameters } from './launch-spec.mjs';
import { loadEffectiveDeclarations, readTrustLedger } from './sources.mjs';

/** One Pi-session owner for all approved MCP connections; no retries of calls. */
export class McpRuntime {
  #options;
  #connections = new Map();
  #entries = [];
  #started = false;
  #stopped = false;
  #starting;
  #prepare;

  constructor(options, { prepare = prepareStdioParameters } = {}) {
    this.#options = options;
    this.#prepare = prepare;
  }

  async start() {
    if (this.#started) throw new Error('MCP runtime already started');
    this.#started = true;
    this.#starting = this.#connectAll();
    await this.#starting;
  }

  async #connectAll() {
    const entries = await loadEffectiveDeclarations(this.#options);
    this.#entries = entries.map((entry) => ({ entry, state: entry.status,
      catalog: undefined, stderrBytes: 0 }));
    for (const record of this.#entries) {
      if (this.#stopped) return;
      if (record.state !== 'approved') continue;
      let transport;
      let client;
      try {
        const parameters = await this.#prepare(record.entry, this.#options.ctx);
        if (this.#stopped) return;
        // Preparation awaits filesystem/credential resolution. A declaration or
        // approval can change during that gap: re-read authority immediately
        // before handing the parameters to the SDK's spawn boundary.
        const current = (await loadEffectiveDeclarations(this.#options)).find((entry) =>
          entry.declaration.id === record.entry.declaration.id);
        if (this.#stopped) return;
        if (!current || current.status !== 'approved' ||
            current.scope !== record.entry.scope || current.digest !== record.entry.digest ||
            current.projectRoot !== record.entry.projectRoot) {
          record.state = current?.status === 'approved' ? 'stale_restart_required'
            : current?.status ?? 'trust_required';
          continue;
        }
        transport = new StdioClientTransport(parameters);
        client = new Client({ name: 'pi-mcp-client', version: '0.1.0' });
        this.#connections.set(record.entry.declaration.id, { client, transport, record });
        transport.stderr?.on('data', (chunk) => {
          // Drain without writing untrusted child bytes into Pi RPC, logs, or the model.
          record.stderrBytes += chunk.length;
        });
        record.state = 'connecting';
        await client.connect(transport, { timeout: 15_000 });
        if (this.#stopped) return;
        record.catalog = await discover(client);
        if (this.#stopped) return;
        record.state = 'ready';
      } catch {
        record.state = 'error'; // No raw SDK/child error text can contain server secrets.
        this.#connections.delete(record.entry.declaration.id);
        await client?.close().catch(() => {});
        await transport?.close().catch(() => {});
      }
    }
  }

  snapshot() {
    return this.#entries.map(({ entry, state, catalog, stderrBytes }) => ({
      id: entry.declaration.id, scope: entry.scope, status: state,
      server: catalog?.server.name, tools: catalog?.tools.length ?? 0,
      resources: catalog?.resources.length ?? 0, prompts: catalog?.prompts.length ?? 0,
      stderrBytes,
    }));
  }

  async #disconnect(id) {
    const active = this.#connections.get(id);
    if (!active) return;
    this.#connections.delete(id);
    active.record.state = 'stale_restart_required';
    try { await active.client.close(); } catch { /* retain fail-closed state */ }
    try { await active.transport.close(); } catch { /* child close was attempted */ }
  }

  /** Recheck the exact declaration and close stale children before remote operations. */
  async authorized(id, ctx) {
    const active = this.#connections.get(id);
    if (!active || active.record.state !== 'ready' || this.#stopped) return false;
    let allowed = false;
    try {
      const current = await loadEffectiveDeclarations({ ...this.#options, ctx });
      allowed = current.some(({ declaration, digest, projectRoot, status, scope }) =>
        declaration.id === id && scope === active.record.entry.scope && status === 'approved' &&
        digest === active.record.entry.digest && projectRoot === active.record.entry.projectRoot);
    } catch {
      // A malformed/uncertain ledger is not permission to keep a child alive.
    }
    if (!allowed) await this.#disconnect(id);
    return allowed;
  }

  /** After a decision write (even an uncertain failure), retire invalid children now. */
  async refresh(ctx) {
    for (const id of [...this.#connections.keys()]) await this.authorized(id, ctx);
  }

  /** Separate out-of-band grant permits noninteractive calls on this exact live declaration. */
  async preauthorized(id, ctx) {
    if (!await this.authorized(id, ctx)) return false;
    const entry = this.#connections.get(id)?.record.entry;
    if (!entry) return false;
    let ledger;
    try { ledger = await readTrustLedger(this.#options.agentDir); }
    catch { await this.#disconnect(id); return false; }
    return callGrantDecision(ledger, entry) === 'allow';
  }

  /** Package-owned lookup for the Pi tool/resource/prompt projection. */
  ready(id) {
    const connection = this.#connections.get(id);
    if (!connection || connection.record.state !== 'ready' || this.#stopped) return undefined;
    return { client: connection.client, catalog: connection.record.catalog };
  }

  async stop() {
    if (this.#stopped) return;
    this.#stopped = true;
    const connections = [...this.#connections.values()];
    this.#connections.clear();
    await Promise.allSettled(connections.map(async ({ client, transport }) => {
      try { await client.close(); } finally { await transport.close(); }
    }));
    await this.#starting?.catch(() => {});
  }
}
