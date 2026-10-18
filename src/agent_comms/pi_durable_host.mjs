// The sole pi-durable importer. All Pi records terminate at this JSONL boundary.
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { requestOwnerTool } from './owner_tool_socket.mjs';
import { BACKGROUND_CONTEXT as context } from '@earendil-works/chord/context';
import { InMemoryCredentialStore } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
import { Harness, ROOT_CONVERSATION_ID, GenerationTask, wrapTool, createRegistry, defineDoc, defineExtension, defineTool, hook, watchEvents } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { NodeExecutionEnv } from '@earendil-works/pi-durable/env/node';
import { CodingTools } from '@earendil-works/pi-durable/tools';

// SQLite evidence and its side files belong to this private host lifetime.
process.umask(0o077);

// 1.3.0 has no public estimator export. Deliberately pinned internal import;
// never reproduce the estimator in Core. This is an explicitly retained API gap.
const { estimateContext } = await import(new URL('./harness/compaction.js', import.meta.resolve('@earendil-works/pi-durable')));
const Recovery = defineDoc({ kind: 'core.recovery', version: 1, scope: 'session', initial: () => ({ decisions: {} }) });
let harness, root, turn, models, registry;
const comms = args => JSON.parse(execFileSync('agent-comms', args, { encoding: 'utf8', timeout: 30000 }));
function ownerGuard(tool, broker) {
  return wrapTool(tool.name, original => ({ ...original,
    async execute(args, api, context) {
      const { path, token } = broker;
      {
        const request = Buffer.from(JSON.stringify({ token,
          call_id: api.callId, name: tool.name, arguments: args }) + '\n');
        if (request.length > 1024 * 1024) throw new Error('Core tool request exceeds the record bound');
        const response = await requestOwnerTool(path, request, 16384,
          'Tool claim outcome UNKNOWN; inspect before retrying.');
        if (response.ok !== true) throw new Error(response.error ?? 'Core owner refused this tool');
      }
      return original.execute(args, api, context);
    },
  }));
}
let resumed = false;
const CoreContext = defineExtension({ name: 'core-context', hooks: [hook(GenerationTask, {
  async beforeRequest({ messages }, api) {
    if (!turn) return;
    const digest = createHash('sha256').update(JSON.stringify(messages)).digest('hex');
    const recorded = await harness.commit(async tx => {
      const proofs = [];
      for (const [inputId, input] of turn.inputs) {
        if (turn.proofs.has(inputId)) continue;
        const submission = await tx.submissionByRequest(root.id, inputId);
        if (!submission?.entry) continue;
        const proof = { inputId, sessionId: String(root.id), sessionEntryId: String(submission.entry),
          requestGeneration: Number(api.taskId), llmContextDigest: digest, committedAtMs: Date.now() };
        const evidence = await tx.appendEntry(root.id, { kind: 'core.input_context',
          data: { context: proof, prompt: input.text } });
        proofs.push([inputId, { context: proof, evidence_entry_id: String(evidence.id) }]);
      }
      return proofs;
    }, context);
    for (const [inputId, proof] of recorded) {
      turn.proofs.set(inputId, proof);
      const now = Date.now();
      emit({ type: 'model_request_progress', progress: { requestId: String(api.taskId),
        sessionId: String(root.id), inputId, startedAtMs: now, observedAtMs: now,
        monotonicNs: String(process.hrtime.bigint()), elapsedMs: 0, callbackMs: 0,
        callbackCount: 0, callbackMaxMs: 0, stage: 'request_start' } });
    }
  },
})] });
const emit = (value) => {
  const { changes, ...update } = value;
  const frames = value.type === 'message_update'
    ? changes.map(assistantMessageEvent => ({ ...update, assistantMessageEvent }))
    : value.type === 'message_end'
      ? (value.entry.model ?? []).map(message => ({ ...value, message })) : [value];
  for (const frame of frames) process.stdout.write(`${JSON.stringify(frame)}\n`);
};
const ack = Object.freeze({});
const requireOpen = () => { if (!harness) throw new Error('Host is not open'); };
const requireResumed = () => { requireOpen(); if (!resumed) throw new Error('Interrupted work must be explicitly abandoned before resume'); };
const workItems = (inspection) => [
  ...inspection.tasks.map(({ record }) => `task:${record.id}`),
  ...inspection.submissions.map(record => `submission:${record.id}`),
];
const text = (entry) => (entry.model ?? []).flatMap(message => typeof message.content === 'string'
  ? [message.content] : (message.content ?? []).filter(block => block.type === 'text').map(block => block.text)).join('\n');
class TurnWatch {
  constructor(stream) {
    this.stream = stream;
    this.inputs = new Map();
    this.proofs = new Map();
    emit(stream.snapshot);
    stream.start(events => {
      for (const event of events) {
        if (event.type === 'submission' && event.record.status === 'placed') {
          const input = this.inputs.get(event.record.requestId);
          if (input) emit(input.started);
        }
        emit(event);
      }
    });
  }
  async submit(request, started) {
    const content = request.images?.length
      ? [{ type: 'text', text: request.text }, ...request.images.map(image => ({ type: 'image', ...image }))]
      : request.text;
    const submission = root.submit({ type: 'input', content,
      requestId: request.requestId, whenBusy: request.whenBusy }, context);
    this.inputs.set(request.requestId, { submission, started, text: request.text });
    return { id: (await submission).id };
  }
  async wait() {
    await (await this.inputs.values().next().value.submission).wait(context);
    await root.waitForIdle(context);
    const records = await Promise.all([...this.inputs.values()].map(async input =>
      (await input.submission).wait(context)));
    const failed = records.find(record => record.status !== 'done');
    const last = records.at(-1);
    const answer = last.status === 'done' ? await root.commit(tx => tx.entry(last.answer), context) : undefined;
    const proof = this.proofs.get(this.inputs.keys().next().value);
    if (failed || !proof) {
      emit({ type: 'durable_turn_failed', text: answer ? text(answer) : '',
        reason: failed?.reason ?? 'Durable turn ended without committed context evidence' });
    } else {
      emit({ type: 'durable_turn_end', text: answer ? text(answer) : '', proof });
    }
    return ack;
  }
  async stop() { await this.stream.stop(); }
}
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
    registry = createRegistry();
    registry.install(CodingTools);
    registry.install(CoreContext);
    harness = await Harness.open(await openNodeSqliteStorage(request.path), {
      models, registry, env: ({ cwd }) => new NodeExecutionEnv({ cwd: cwd ?? request.cwd }),
      onReport: error => process.stderr.write(`${error.stack ?? error}\n`),
    }, context);
    const agent = { cwd: request.cwd };
    if (request.provider) agent.model = { provider: request.provider, modelId: request.modelId };
    if (request.thinkingLevel) agent.thinkingLevel = request.thinkingLevel;
    root = await harness.root(context, { agent });
    await root.configure({ extensions: { add: [CoreContext] } }, context);
    return { work: workItems(await harness.inspect(context)) };
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
    return ack;
  },
  async watch() {
    requireOpen();
    if (turn) throw new Error('A durable turn watch is already open');
    turn = new TurnWatch(await watchEvents(harness, root.id, context));
    return ack;
  },
  async unwatch() {
    requireOpen();
    if (!turn) throw new Error('No durable turn watch is open');
    await turn.stop();
    turn = undefined;
    return ack;
  },
  async inspect() { requireOpen(); return { work: workItems(await harness.inspect(context)) }; },
  async settle(request) {
    requireOpen();
    if (resumed) throw new Error('Recovery is only available before resume');
    if (request.disposition !== 'ABANDON') throw new Error('Only explicit ABANDON is supported');
    const work = workItems(await harness.inspect(context)).find(item => item === request.work);
    if (!work) throw new Error('Interrupted work does not exist');
    await harness.commit(async tx => { (await tx.doc(Recovery)).decisions[work] = 'ABANDON'; }, context);
    const [kind, id] = work.split(':');
    if (kind === 'task') await harness.abortTask(Number(id), context);
    else await harness.abortSubmission(Number(id), context);
    return ack;
  },
  async resume() {
    requireOpen();
    if (resumed) return ack;
    const inspection = await harness.inspect(context);
    const decisions = (await harness.snapshot(Recovery, context))?.decisions ?? {};
    if (workItems(inspection).some(work => decisions[work] !== 'ABANDON')) throw new Error('Interrupted work has no recorded disposition');
    if (inspection.tasks.some(({ record }) => !record.abortRequested)) throw new Error('Interrupted task was not abort-marked');
    harness.resume();
    resumed = true;
    return ack;
  },
  async submit(request) {
    requireResumed();
    if (typeof request.requestId !== 'string' || !request.requestId) throw new Error('Core InputId is required as requestId');
    return turn.submit(request, { type: 'durable_following_started', id: request.requestId,
      native_id: request.requestId, text: request.text });
  },
  async submit_initial(request) {
    requireResumed();
    return turn.submit(request, { type: 'durable_original_started', native_id: request.requestId, text: request.text });
  },
  async await_turn() { requireResumed(); return turn.wait(); },
  async abort() { requireResumed(); await root.abort(context); return ack; },
  async entries(request) {
    requireOpen();
    const { minEntryId, maxEntryId, order } = request;
    const page = await root.entries({ minEntryId, maxEntryId, order }, request.limit, request.cursor ?? undefined, context);
    return { items: page.items, cursor: page.next ?? null };
  },
  async get_session_stats() {
    requireOpen();
    const agent = await root.agent(context);
    const model = agent.model && models.getModel(agent.model.provider, agent.model.modelId);
    return { contextUsage: { tokens: estimateContext(await root.context(context), []), contextWindow: model?.contextWindow ?? null } };
  },
  async configure_tools(request) {
    requireOpen();
    const declarations = comms(['tools']).tools;
    const commands = defineExtension({ name: 'core-commands', tools: declarations.map(tool => defineTool({
      name: tool.name, description: tool.description, parameters: tool.parameters,
      async execute(args) {
        const result = comms(['tool', '--from', process.env.AGENT_COMMS_THREAD,
          '--tool', tool.name, '--arguments', JSON.stringify(args)]);
        return { output: [{ type: 'text', text: JSON.stringify(result) }], details: result };
      },
    })) });
    registry.install(commands);
    const extensions = [CodingTools, CoreContext, commands];
    for (const broker of request.brokers) {
      if (!broker.path || !/^[0-9a-f]{64}$/.test(broker.token)) {
        throw new Error('Brokered Durable tools require an authenticated owner endpoint');
      }
      const owner = defineExtension({ name: 'core-tools', wraps:
        [...CodingTools.tools, ...commands.tools].map(tool => ownerGuard(tool, broker)) });
      registry.install(owner);
      extensions.push(owner);
    }
    await root.configure({ extensions, tools: request.tools }, context);
    const actual = new Set((await root.agent(context)).callable.map(tool => tool.name));
    for (const name of request.tools) if (!actual.has(name)) throw new Error(`Durable tool is unavailable: ${name}`);
    return ack;
  },
  async get_identity() { requireOpen(); return { sessionId: String(root.id) }; },
  async get_state() {
    requireOpen();
    const agent = await root.agent(context);
    return { model: agent.model ? models.getModel(agent.model.provider, agent.model.modelId) : null, thinkingLevel: agent.thinkingLevel };
  },
  async get_available_models() { requireOpen(); return { models: await models.getAvailable() }; },
  async set_model(request) {
    requireOpen();
    await root.configure({ model: { provider: request.provider, modelId: request.modelId } }, context);
    return ack;
  },
  async set_thinking_level(request) {
    requireOpen();
    await root.configure({ thinkingLevel: request.level }, context);
    return ack;
  },
  async compact_durable(request) {
    requireResumed();
    const task = await harness.waitForTask(await root.compact(request.customInstructions, context), context);
    const outcome = task.state.outcome;
    if (outcome.status !== 'completed') return { outcome: { kind: 'failed', message: JSON.stringify(outcome) } };
    if (!outcome.result.submissionId) return { outcome: { kind: 'nothing_to' } };
    const placed = await (await harness.submission(outcome.result.submissionId, context)).wait(context);
    if (placed.status !== 'done') return { outcome: placed.reason === 'stale' ? { kind: 'stale' } : { kind: 'failed', message: JSON.stringify(placed) } };
    const entry = await root.commit(tx => tx.entry(placed.entry), context);
    return { outcome: { kind: 'placed', summary: text(entry), first_kept: String(entry.head), tokens_before: null } };
  },
  async close() {
    if (turn) await turn.stop();
    if (harness) await harness.close(context);
    harness = undefined;
    return ack;
  },
};
const lines = createInterface({ input: process.stdin });
// Commands may wait for work while abort/steering must remain deliverable.
for await (const line of lines) {
  let request;
  try { request = JSON.parse(line); } catch (error) { process.stderr.write(`${error}\n`); process.exitCode = 1; break; }
  void (async () => {
    try {
      const handler = handlers[request.type];
      if (!handler) throw new Error(`Unknown durable command: ${request.type}`);
      emit({ type: 'response', id: request.id, command: request.type, success: true, data: await handler(request) });
    } catch (error) { emit({ type: 'response', id: request.id, command: request.type, success: false, error: error.stack ?? String(error) }); }
  })();
}
if (turn) await turn.stop();
if (harness) await harness.close(context);
