// The sole pi-durable importer. All Pi records terminate at this JSONL boundary.
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import { BACKGROUND_CONTEXT as context } from '@earendil-works/chord/context';
import { InMemoryCredentialStore } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
import { Harness, ROOT_CONVERSATION_ID, createRegistry, defineDoc, watchEvents } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { NodeExecutionEnv } from '@earendil-works/pi-durable/env/node';
import { CodingTools } from '@earendil-works/pi-durable/tools';

// 1.3.0 has no public estimator export. Deliberately pinned internal import;
// never reproduce the estimator in Core. This is an explicitly retained API gap.
const { estimateContext } = await import(new URL('./harness/compaction.js', import.meta.resolve('@earendil-works/pi-durable')));
const Recovery = defineDoc({ kind: 'core.recovery', version: 1, scope: 'session', initial: () => ({ decisions: {} }) });
let harness, root, stream, models;
let resumed = false;
const emit = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const requireOpen = () => { if (!harness) throw new Error('Host is not open'); };
const requireResumed = () => { requireOpen(); if (!resumed) throw new Error('Interrupted work must be explicitly abandoned before resume'); };
const workItems = (inspection) => [
  ...inspection.tasks.map(({ record }) => ({ id: `task:${record.id}`, kind: record.kind })),
  ...inspection.submissions.map(record => ({ id: `submission:${record.id}`, kind: 'submission' })),
];
const text = (entry) => (entry.model ?? []).flatMap(message => typeof message.content === 'string'
  ? [message.content] : (message.content ?? []).filter(block => block.type === 'text').map(block => block.text)).join('\n');
const handlers = {
  async open(request) {
    if (harness) throw new Error('Host already open');
    const credentials = new InMemoryCredentialStore();
    // Verification uses an isolated credential snapshot. Never mutate original auth.
    if (request.authPath) {
      for (const [provider, credential] of Object.entries(JSON.parse(await readFile(request.authPath, 'utf8')))) {
        await credentials.modify(provider, async () => credential);
      }
    }
    models = builtinModels({ credentials });
    const registry = createRegistry();
    registry.install(CodingTools);
    harness = await Harness.open(await openNodeSqliteStorage(request.path), {
      models, registry, env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? request.cwd }),
      onReport: error => process.stderr.write(`${error.stack ?? error}\n`),
    }, context);
    root = await harness.root(context, { agent: { cwd: request.cwd, ...(request.agent ?? {}) } });
    const initial = await watchEvents(harness, root.id, context);
    await initial.stop();
    return { snapshot: initial.snapshot, interrupted: workItems(await harness.inspect(context)) };
  },
  async open_history(request) {
    if (harness) throw new Error('Host already open');
    models = builtinModels({ credentials: new InMemoryCredentialStore() });
    harness = await Harness.open(await openNodeSqliteStorage(request.path), {
      models, registry: createRegistry(),
      env: ({ cwd }) => new NodeExecutionEnv({ cwd }),
      onReport: error => process.stderr.write(`${error.stack ?? error}\n`),
    }, context);
    root = await harness.conversation(ROOT_CONVERSATION_ID, context);
    if (!root) throw new Error('Durable database has no root conversation');
    return null;
  },
  async watch() {
    requireOpen();
    if (stream) throw new Error('A durable turn watch is already open');
    stream = await watchEvents(harness, root.id, context);
    emit({ event: stream.snapshot });
    stream.start(async events => { for (const event of events) emit({ event }); });
    return null;
  },
  async unwatch() {
    requireOpen();
    if (!stream) throw new Error('No durable turn watch is open');
    await stream.stop();
    stream = undefined;
    return null;
  },
  async inspect() { requireOpen(); return workItems(await harness.inspect(context)); },
  async settle(request) {
    requireOpen();
    if (resumed) throw new Error('Recovery is only available before resume');
    if (request.disposition !== 'ABANDON') throw new Error('Only explicit ABANDON is supported');
    const work = workItems(await harness.inspect(context)).find(item => item.id === request.work);
    if (!work) throw new Error('Interrupted work does not exist');
    await harness.commit(async tx => { (await tx.doc(Recovery)).decisions[work.id] = 'ABANDON'; }, context);
    const [kind, id] = work.id.split(':');
    if (kind === 'task') await harness.abortTask(Number(id), context);
    else await harness.abortSubmission(Number(id), context);
    return { id: work.id, disposition: 'ABANDON' };
  },
  async resume() {
    requireOpen();
    if (resumed) return null;
    const inspection = await harness.inspect(context);
    const decisions = (await harness.snapshot(Recovery, context))?.decisions ?? {};
    if (workItems(inspection).some(work => decisions[work.id] !== 'ABANDON')) throw new Error('Interrupted work has no recorded disposition');
    if (inspection.tasks.some(({ record }) => !record.abortRequested)) throw new Error('Interrupted task was not abort-marked');
    harness.resume();
    resumed = true;
    return null;
  },
  async submit(request) {
    requireResumed();
    if (typeof request.requestId !== 'string' || !request.requestId) throw new Error('Core InputId is required as requestId');
    const submission = await root.submit({ type: 'input', content: request.content, requestId: request.requestId, whenBusy: request.whenBusy }, context);
    return { id: submission.id };
  },
  async abort() { requireResumed(); await root.abort(context); return null; },
  async entries(request) {
    requireOpen();
    const page = await root.entries(request.query ?? {}, request.limit, request.cursor ?? undefined, context);
    return { items: page.items.map(entry => ({ id: String(entry.id), order: entry.id, kind: ({ 'pi.user': 'user', 'pi.assistant': 'assistant', 'pi.tool-result': 'tool_result', 'pi.compaction': 'compaction', 'pi.reset': 'reset' })[entry.kind] ?? 'system', text: text(entry) })), cursor: page.cursor };
  },
  async context_usage() {
    requireOpen();
    const agent = await root.agent(context);
    const model = agent.model && models.getModel(agent.model.provider, agent.model.modelId);
    return { tokens: estimateContext(await root.context(context), []), window: model?.contextWindow ?? null };
  },
  async configure(request) {
    requireOpen();
    if (request.agent) await root.configure(request.agent, context);
    const agent = await root.agent(context);
    return { model: agent.model, thinking: agent.thinkingLevel, models: (await models.getAvailable()).map(model => `${model.provider}/${model.id}`) };
  },
  async compact(request) {
    requireResumed();
    const task = await harness.waitForTask(await root.compact(request.instructions, context), context);
    const outcome = task.state.outcome;
    if (outcome.status !== 'completed') return { status: 'failed', message: JSON.stringify(outcome) };
    if (!outcome.result.submissionId) return { status: 'nothing' };
    const placed = await (await harness.submission(outcome.result.submissionId, context)).wait(context);
    if (placed.status !== 'done') return { status: placed.reason === 'stale' ? 'stale' : 'failed', message: JSON.stringify(placed) };
    const entry = await root.commit(tx => tx.entry(placed.entry), context);
    return { status: 'placed', summary: text(entry), first_kept: String(entry.head), tokens_before: null };
  },
  async close() {
    if (stream) await stream.stop();
    if (harness) await harness.close(context);
    harness = undefined;
    return null;
  },
};
const lines = createInterface({ input: process.stdin });
// Commands may wait for work while abort/steering must remain deliverable.
for await (const line of lines) {
  let request;
  try { request = JSON.parse(line); } catch (error) { emit({ error: String(error) }); continue; }
  void (async () => {
    try {
      const handler = handlers[request.command];
      if (!handler) throw new Error(`Unknown durable command: ${request.command}`);
      emit({ id: request.id, result: await handler(request) });
    } catch (error) { emit({ id: request.id, error: error.stack ?? String(error) }); }
  })();
}
if (stream) await stream.stop();
if (harness) await harness.close(context);
