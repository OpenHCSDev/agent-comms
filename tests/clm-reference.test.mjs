// The native CLM extension against the reference harness it ports
// (facebookresearch/context-language-models clm/clm_harness at 3c89408). GOLDEN is that
// harness's own output on the same inputs: render_editable, parse_back and the budget
// controller's messages, so any drift from what the checkpoint was trained on fails here.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import clmReference, {
  BudgetController, BudgetPressure, ContextEditPolicy, DigestEachOutput, EditRule, Seen,
  acceptanceProblems, addedRepeats, citedEvidenceMissing, literals, normalizedTurns,
  renderEditable, repeatedTurnIndices,
} from "../src/agent_comms/execution/clm_reference.mjs";

const GOLDEN = {"rendered": "[[CTX_TURN 1 role=assistant]]\nthink a\nTHOUGHT: look\nbash {\"command\": \"ls\"}\n\n[[CTX_TURN 2 role=tool]]\na.txt\n\n(exit_code=0)\n\n[[CTX_TURN 3 role=assistant]]\ndone", "edits": ["[[CTX_TURN 1 role=assistant]]\nthink a\nTHOUGHT: look\nbash {\"command\": \"ls\"}\n\n[[CTX_TURN 2 role=tool]]\na.txt\n\n(exit_code=0)\n\n[[CTX_TURN 3 role=assistant]]\ndone", "lead note\n\n[[CTX_TURN 1 role=assistant]]\nkept\n\n[[CTX_TURN 2 role=tool]]\n\n[[CTX_TURN 3 role=notes]]\nmy notes\n\n[[CTX_TURN 4 role=user]]\nmore", "no headers at all\njust text", "[[CTX_TURN 1 role=assistant]]\na\n[[CTX_TURN 2 role=assistant]]\nb\n[[CTX_TURN 3 role=tool]]\nt\n[[CTX_TURN 4 role=system]]\ns", ""], "parsed": [[{"role": "assistant", "content": "think a\nTHOUGHT: look\nbash {\"command\": \"ls\"}"}, {"role": "user", "content": "a.txt\n\n(exit_code=0)"}, {"role": "assistant", "content": "done"}], [{"role": "user", "content": "lead note"}, {"role": "assistant", "content": "kept"}, {"role": "user", "content": "my notes\n\nmore"}], [{"role": "user", "content": "no headers at all\njust text"}], [{"role": "assistant", "content": "a\n\nb"}, {"role": "user", "content": "t\n\ns"}], []], "nudges": {"0.25": "CONTEXT BUDGET NUDGE: context is at ~25% of your 28672-token budget (12345 tokens). No action needed. Before your next few searches, make sure your notes record which queries and documents you already tried.", "0.5": "CONTEXT BUDGET NUDGE: context is at ~50% of your 28672-token budget (12345 tokens). Finish the unit of work in flight, then tidy ONCE. When you write a replacement note, COPY facts forward from the text you are replacing (quote them): every RULED OUT candidate with its reason and the words 'do not retry'; the exact queries/commands already tried; exact values marked VERIFIED or UNVERIFIED; and a NEXT line. HINT", "0.75": "CONTEXT BUDGET NUDGE: context is at ~75% of your 28672-token budget (12345 tokens) \u2014 close to the limit. Compact settled spans now \u2014 but do NOT wipe: edits keeping under 25% of the region they touch are usually followed by re-doing the deleted work. When you write a replacement note, COPY facts forward from the text you are replacing (quote them): every RULED OUT candidate with its reason and the words 'do not retry'; the exact queries/commands already tried; exact values marked VERIFIED or UNVERIFIED; and a NEXT line. if you cross the limit you get one final turn and then the session ends. HINT"}, "rollback": "[SYSTEM NOTICE \u2014 CONTEXT LIMIT HIT (retry 2/50)] Your context crossed the 28672-token limit, so your 3 most recent turn(s) were ROLLED BACK and are gone \u2014 that work is no longer in your context and cannot be recovered (7 turn(s) lost so far). You are now at ~20000/26624 tokens, which leaves room for exactly one thing: CONDENSE YOUR CONTEXT THIS TURN and do nothing else. Replace stale regions with short summaries. HINT\nThese commands already ran and their output is what blew your budget, so their turns were discarded. Re-running them will just lose the context again \u2014 if you need one, make it print far less (head/grep/count instead of dumping):\n  - ls -R /\n  - cat big.log", "final": "[SYSTEM NOTICE] You have reached your context budget (30000/28672 tokens). This is your FINAL turn: make sure your solution is complete and in place \u2014 write any required output/answer to the location the task specifies (or run your submit command) \u2014 because after this turn the session ends and your work is evaluated as-is.", "urgent": "CONTEXT BUDGET NUDGE (URGENT): You are at 25000/26624 tokens \u2014 over 90% of your hard context limit and about to be cut off. Compact your context THIS TURN (do nothing else): remove stale regions now. If you cross the limit you get exactly one final turn and then the session ends with your work graded as-is. HINT", "strict_target": 26624};

const user = (text) => ({ role: "user", content: [{ type: "text", text }], timestamp: 1 });
const assistant = (content, extra = {}) => ({
  role: "assistant", content, api: "openai-completions", provider: "clm-local", model: "m",
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, stopReason: "toolUse", timestamp: 1, ...extra,
});
const toolResult = (text) => ({ role: "toolResult", toolCallId: "c1", toolName: "bash",
  content: [{ type: "text", text }], isError: false, timestamp: 1 });

test("the mirror renders Pi turns exactly as the reference renders the same chat", () => {
  const messages = [
    user("TASK"),
    assistant([{ type: "thinking", thinking: "think a" }, { type: "text", text: "THOUGHT: look" },
               { type: "toolCall", id: "c1", name: "bash", arguments: { command: "ls" } }]),
    toolResult("a.txt\n\n(exit_code=0)"),
    assistant([{ type: "text", text: "done" }]),
  ];
  assert.equal(renderEditable(messages, 1), GOLDEN.rendered);
});

test("an edited mirror parses back to the reference's turns", () => {
  GOLDEN.edits.forEach((edit, i) => {
    assert.deepEqual(normalizedTurns(edit).map((t) => ({ role: t.role, content: t.text })), GOLDEN.parsed[i]);
  });
});

test("budget notices are the reference's words", () => {
  const budget = new BudgetController({ budget: 28672, reserve: 2048, compactionHint: "HINT" });
  assert.equal(budget.strictTarget, GOLDEN.strict_target);
  for (const [frac, text] of Object.entries(GOLDEN.nudges)) assert.equal(budget.nudgeMessage(Number(frac), 12345), text);
  budget.nRetry = 2;
  budget.nTurnsRolledBack = 7;
  assert.equal(budget.rollbackMessage(3, 20000, ["ls -R /", "cat big.log"]), GOLDEN.rollback);
  assert.equal(budget.finalMessage(30000), GOLDEN.final);
  assert.equal(budget.persistentNudgeMessage(25000, 0.9), GOLDEN.urgent);
});

/** A Pi stand-in: one session's branch, the hooks the extension registered, appendEntry. */
function harness(sessionId) {
  const hooks = {};
  const branch = [];
  const pi = {
    on: (name, fn) => { hooks[name] = fn; },
    appendEntry: (customType, data) => branch.push({ type: "custom", customType, data }),
  };
  const ctx = {
    sessionManager: { getSessionId: () => sessionId, getBranch: () => branch },
    getSystemPrompt: () => "SYSTEM",
    abort: () => { ctx.aborted = true; },
  };
  clmReference(pi);
  return { hooks, ctx, branch };
}

async function turn(h, raw, command, editTo) {
  const { messages } = await h.hooks.context({ messages: raw }, h.ctx);
  await h.hooks.turn_start({}, h.ctx);
  const input = { command };
  await h.hooks.tool_call({ toolName: "bash", toolCallId: "c1", input }, h.ctx);
  const file = /LIVE_CTX_MAIN\.txt/.test(command) ? command.match(/(\S+LIVE_CTX_MAIN\.txt)/)[1] : null;
  if (file && editTo) writeFileSync(file, editTo(readFileSync(file, "utf8")));
  const result = await h.hooks.tool_result({ toolName: "bash", content: [{ type: "text", text: "" }], isError: false }, h.ctx);
  return { sent: messages, input, result: result.content[0].text };
}

test("an applied edit replaces the editable region from the next request on, and survives a restart", async () => {
  const session = `t${process.pid}a`;
  const h = harness(session);
  await h.hooks.session_start({}, h.ctx);
  const prompt = await h.hooks.before_agent_start(
    { systemPrompt: "pi default", systemPromptOptions: { appendSystemPrompt: "THE DEAL" } }, h.ctx);
  assert.doesNotMatch(prompt.systemPrompt, /THE DEAL/);
  assert.match(prompt.systemPrompt, /mirrored to `\/tmp\/\.live_ctx\/.+\/LIVE_CTX_MAIN\.txt`/);
  assert.doesNotMatch(prompt.systemPrompt, /pi default/);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const raw = [user("TASK"), assistant([{ type: "toolCall", id: "c0", name: "bash", arguments: { command: "cat big" } }]),
               toolResult("x".repeat(4000) + "\n\n(exit_code=0)")];
  const call = assistant([{ type: "toolCall", id: "c1", name: "bash", arguments: { command: `python3 edit ${file}` } }]);
  raw.push(call);
  const edited = await turn(h, raw.slice(0, 3), `python3 edit ${file}`,
    () => "[[CTX_TURN 1 role=assistant]]\nran cat big\n\n[[CTX_TURN 2 role=tool]]\n[big output: 4000 x]");
  assert.equal(edited.input.command, `python3 edit ${file}`);
  assert.match(edited.result, /LIVE_CTX_MAIN\.txt: edit applied — context ~\d+->\d+ tokens, 3 turns/);
  assert.match(edited.result, /^\(no output\)\n\n\(exit_code=0\)\n\[context: ~\d+\/26624 tokens\]/);
  raw.push(toolResult("ok"));
  const next = (await h.hooks.context({ messages: raw }, h.ctx)).messages;
  assert.deepEqual(next.map((m) => m.role), ["user", "assistant", "user", "assistant", "toolResult"]);
  assert.equal(next[2].content[0].text, "[big output: 4000 x]");
  const again = harness(session);
  await again.hooks.session_start({}, { ...again.ctx, sessionManager: { ...again.ctx.sessionManager, getBranch: () => h.branch } });
  const rebuilt = (await again.hooks.context({ messages: raw }, { ...again.ctx, sessionManager: { ...again.ctx.sessionManager, getBranch: () => h.branch } })).messages;
  assert.deepEqual(rebuilt.map((m) => m.content[0]?.text), next.map((m) => m.content[0]?.text));
});

test("an edit that grows past the limit is rejected with the reference's note", async () => {
  process.env.CLM_CONTEXT_BUDGET = "4096";
  const session = `t${process.pid}b`;
  const h = harness(session);
  delete process.env.CLM_CONTEXT_BUDGET;
  await h.hooks.session_start({}, h.ctx);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const raw = [user("TASK"), assistant([{ type: "text", text: "hi" }])];
  const { result } = await turn(h, raw, `sed -i x ${file}`, (s) => s + "\n" + "y".repeat(40000));
  assert.match(result, /LIVE_CTX_MAIN\.txt: edit REJECTED — it GREW context ~\d+->\d+ tokens, so it was NOT applied .*An edit must FIT the 2048-token limit/);
});

test("crossing the limit rolls the newest turns back and pins the ledger", async () => {
  // The reference rolls back only while strict_target exceeds the 2,048-token retry margin.
  process.env.CLM_CONTEXT_BUDGET = "8192";
  const h = harness(`t${process.pid}c`);
  delete process.env.CLM_CONTEXT_BUDGET;
  await h.hooks.session_start({}, h.ctx);
  const raw = [user("TASK")];
  for (let i = 0; i < 4; i++) {
    raw.push(assistant([{ type: "toolCall", id: `c${i}`, name: "bash", arguments: { command: `dump ${i}` } }]));
    raw.push(toolResult("z".repeat(8000)));
  }
  const { messages } = await h.hooks.context({ messages: raw }, h.ctx);
  assert.match(messages[1].content[0].text, /^\[SYSTEM NOTICE — CONTEXT LIMIT HIT \(retry 1\/50\)\]/);
  assert.match(messages[1].content[0].text, /  - dump 3/);
  assert.ok(messages.length < raw.length + 1);
  assert.notEqual(messages[messages.length - 1].role, "assistant");
});

test("a rollback never removes an input: inputs are the task", async () => {
  process.env.CLM_CONTEXT_BUDGET = "8192";
  const h = harness(`t${process.pid}d`);
  delete process.env.CLM_CONTEXT_BUDGET;
  await h.hooks.session_start({}, h.ctx);
  const raw = [user("TASK")];
  for (let i = 0; i < 4; i++) {
    raw.push(assistant([{ type: "toolCall", id: `c${i}`, name: "bash", arguments: { command: `dump ${i}` } }]));
    raw.push(toolResult("z".repeat(8000)));
  }
  raw.push({ ...user("the next input"), inputId: "a".repeat(32) });
  const { messages } = await h.hooks.context({ messages: raw }, h.ctx);
  assert.match(messages[1].content[0].text, /CONTEXT LIMIT HIT/);
  assert.equal(messages[messages.length - 1].inputId, "a".repeat(32));
});

test("each request publishes the live view it sent, atomically, for the owner to record", async () => {
  const session = `t${process.pid}e`;
  const h = harness(session);
  await h.hooks.session_start({}, h.ctx);
  const raw = [user("TASK"), assistant([{ type: "thinking", thinking: "plan" },
    { type: "toolCall", id: "c0", name: "bash", arguments: { command: "ls" } }]), toolResult("a.txt")];
  const { messages } = await h.hooks.context({ messages: raw }, h.ctx);
  const view = JSON.parse(readFileSync(`/tmp/.live_ctx/${session}/LIVE_VIEW.json`, "utf8"));
  assert.equal(view.version, 1);
  assert.equal(view.system, "SYSTEM");
  assert.deepEqual(view.turns.map((turn) => turn.role), ["user", "assistant", "tool"]);
  assert.equal(view.turns[1].text, 'plan\nbash {"command": "ls"}');
  assert.equal(view.turns.length, messages.length);
  assert.equal(view.budget.strict_target, 26624);
});

/** One model step: the request, the command, and the command's output as Pi delivers it. */
async function step(h, raw, command, output, editTo) {
  await h.hooks.context({ messages: raw }, h.ctx);
  await h.hooks.turn_start({}, h.ctx);
  const refused = await h.hooks.tool_call({ toolName: "bash", toolCallId: "c1", input: { command } }, h.ctx);
  if (refused?.block) return { refused: refused.reason };
  const file = command.match(/(\S+LIVE_CTX_MAIN\.txt)/)?.[1];
  if (file && editTo) writeFileSync(file, editTo(readFileSync(file, "utf8")));
  const result = await h.hooks.tool_result(
    { toolName: "bash", content: [{ type: "text", text: output }], isError: false }, h.ctx);
  return { result: result.content[0].text };
}

/** The harness under one provider environment. */
async function under(environment, session, body) {
  const before = { ...process.env };
  Object.assign(process.env, environment);
  try {
    const h = harness(session);
    await h.hooks.session_start({}, h.ctx);
    await body(h, `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`);
  } finally {
    for (const name of Object.keys(environment)) {
      if (name in before) process.env[name] = before[name]; else delete process.env[name];
    }
  }
}

const read = (command) => assistant([{ type: "toolCall", id: "c1", name: "bash", arguments: { command } }]);

test("an edit rule is named by its declaration, and the reference rule never refuses", () => {
  assert.ok(EditRule.named("budget") instanceof BudgetPressure);
  assert.ok(EditRule.named("digest") instanceof BudgetPressure);
  assert.throws(() => EditRule.named("nonsense"), /Unknown CLM edit rule/);
  const reference = EditRule.named("budget");
  reference.observed(5000);
  assert.equal(reference.refusal("cat more", "F"), null);
  assert.equal(reference.reminder("F"), "");
});

test("a required context edit is a turn-completion policy, not another output-digest rule", async () => {
  assert.equal(ContextEditPolicy.named("optional").instruction("F"), "");
  assert.throws(
    () => ContextEditPolicy.named("nonsense"), /Unknown CLM context-edit policy/,
  );

  await under({ CLM_CONTEXT_EDIT_POLICY: "required_each_turn" }, `t${process.pid}policy`, async (h, file) => {
    const prompt = await h.hooks.before_agent_start({}, h.ctx);
    assert.match(prompt.systemPrompt, /Before your final answer, you MUST apply one accepted edit/);
    assert.match(prompt.systemPrompt, new RegExp(file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

    await h.hooks.agent_start({}, h.ctx);
    const final = assistant([{ type: "text", text: "done" }], { stopReason: "stop" });
    const refused = await h.hooks.message_end({ message: final }, h.ctx);
    assert.equal(refused.message.stopReason, "error");
    assert.match(refused.message.errorMessage, /ended without an accepted LIVE_CTX_MAIN\.txt edit/);

    const raw = [user("TASK"), assistant([{ type: "text", text: "settled detail" }])];
    const edited = await turn(h, raw, `python3 edit ${file}`,
      (text) => text.replace("settled detail", "durable note"));
    assert.match(edited.result, /edit applied/);
    assert.equal(await h.hooks.message_end({ message: final }, h.ctx), undefined);
  });
});

test("under the digest rule a sizeable output must be noted and dropped before the next command runs", async () => {
  await under({ CLM_EDIT_RULE: "digest" }, `t${process.pid}g`, async (h, file) => {
    const chunk = "paper text ".repeat(300); // ~825 tokens
    const raw = [user("Read the paper"), read("sed -n 1,80p paper.txt")];
    const first = await step(h, raw, "sed -n 1,80p paper.txt", chunk);
    assert.match(first.result, /DIGEST FIRST: ~\d+ tokens of command output are still raw/);
    assert.match(first.result, /python3 \/tmp\/\.live_ctx\/\S+\/digest\.py <<'NOTES'\n<replace this line with your own notes on that output>\nNOTES/);

    raw.push(toolResult(first.result), read("sed -n 81,160p paper.txt"));
    const next = await step(h, raw, "sed -n 81,160p paper.txt", chunk);
    assert.match(next.refused, /^COMMAND NOT RUN\. Nothing is wrong with your command or the file/);
    assert.match(next.refused, /digest\.py <<'NOTES'/);

    // Writing the same text back frees nothing: still refused afterwards.
    const same = await step(h, raw, `python3 rewrite.py ${file}`, "", (text) => text);
    assert.match(same.result, /DIGEST FIRST/);
    assert.match((await step(h, raw, "sed -n 81,160p paper.txt", chunk)).refused, /COMMAND NOT RUN/);

    // Notes in place of the raw output digest it: the next read runs.
    const noted = await step(h, raw, `python3 note.py ${file}`, "",
      (text) => text.replace(/paper text (paper text )+/g, "NOTE: section 1 defines the mirror file. "));
    assert.match(noted.result, /edit applied/);
    assert.doesNotMatch(noted.result, /DIGEST FIRST/);
    const after = await step(h, raw, "sed -n 81,160p paper.txt", "short");
    assert.equal(after.refused, undefined);
  });
});

test("the reference rule lets the same commands run undigested", async () => {
  await under({ CLM_EDIT_RULE: "budget" }, `t${process.pid}h`, async (h) => {
    const raw = [user("Read the paper"), read("sed -n 1,80p paper.txt")];
    const first = await step(h, raw, "sed -n 1,80p paper.txt", "paper text ".repeat(300));
    assert.doesNotMatch(first.result, /DIGEST FIRST/);
    raw.push(toolResult(first.result), read("sed -n 81,160p paper.txt"));
    assert.equal((await step(h, raw, "sed -n 81,160p paper.txt", "x")).refused, undefined);
  });
});

test("an output that would not fit is stored and described, not shown, and the turn goes on", async () => {
  await under({ CLM_CONTEXT_BUDGET: "4096", CLM_CONTEXT_RESERVE: "2048" }, `t${process.pid}s`, async (h) => {
    const big = Array.from({ length: 400 }, (_, line) => `line ${line} of the log`).join("\n"); // ~2,000 tokens
    const raw = [user("TASK ".repeat(200)), read("cat big.log")];
    const stored = await step(h, raw, "cat big.log", big);

    const file = stored.result.match(/saved in (\S+)\./)[1];
    assert.match(stored.result, /OUTPUT STORED, NOT SHOWN: this command's output is ~\d+ tokens \(400 lines\)/);
    assert.match(stored.result, /\(exit_code=0\)/);
    assert.doesNotMatch(stored.result, /line 200 of the log/);
    assert.equal(readFileSync(file, "utf8"), big);
    assert.doesNotMatch(file, /\.live_ctx/);

    // A small output is shown even with no room left: storing it would cost more than it holds.
    raw.push(toolResult(stored.result), read("echo ok"));
    assert.match((await step(h, raw, "echo ok", "ok")).result, /^ok\n\n\(exit_code=0\)/);
  });
});

test("under the digest rule a stored output is not counted as raw text in the context", async () => {
  await under({ CLM_EDIT_RULE: "digest", CLM_CONTEXT_BUDGET: "4096", CLM_CONTEXT_RESERVE: "2048" },
    `t${process.pid}d`, async (h) => {
      const big = Array.from({ length: 400 }, (_, line) => `line ${line} of the log`).join("\n");
      const raw = [user("TASK ".repeat(200)), read("cat big.log")];
      const stored = await step(h, raw, "cat big.log", big);
      assert.match(stored.result, /OUTPUT STORED/);
      assert.doesNotMatch(stored.result, /DIGEST FIRST/);
    });
});

test("an edit that drops the request being worked on has it restored; an older request may go", async () => {
  await under({}, `t${process.pid}k`, async (h, file) => {
    const first = user("FIRST TASK: sum 1..3000");
    const older = { ...user("Timing probe: reply SWITCH-2"), inputId: "i1" };
    const current = { ...user("Read the paper at paper.txt and summarise it"), inputId: "i2" };
    const raw = [first, older, assistant([{ type: "text", text: "SWITCH-2" }], { stopReason: "stop" }),
                 current, read("sed -n 1,80p paper.txt"), toolResult("paper text"), read(`python3 wipe.py ${file}`)];
    const wiped = await step(h, raw, `python3 wipe.py ${file}`, "",
      () => "[[CTX_TURN 1 role=user]]\nNOTES: nothing kept");

    assert.match(wiped.result, /your edit removed the request you are working on; it was restored/);
    raw.push(toolResult(wiped.result));
    const { messages } = await h.hooks.context({ messages: raw }, h.ctx);
    const texts = messages.map((message) => message.content.map((part) => part.text ?? "").join(""));
    assert.ok(texts.some((text) => text.includes("Read the paper at paper.txt and summarise it")));
    assert.ok(!texts.some((text) => text.includes("Timing probe: reply SWITCH-2")));
    assert.ok(texts[0].includes("FIRST TASK"));
  });
});

test("an edit that keeps the current request is applied as written", async () => {
  await under({}, `t${process.pid}m`, async (h, file) => {
    const current = { ...user("Read the paper"), inputId: "i2" };
    const raw = [user("FIRST"), current, read("ls"), toolResult("a.txt"), read(`python3 note.py ${file}`)];
    const kept = await step(h, raw, `python3 note.py ${file}`, "", (text) => text.replace("a.txt", "one file"));
    assert.match(kept.result, /edit applied/);
    assert.doesNotMatch(kept.result, /restored/);
  });
});

test("the digest command replaces the largest raw output with the notes it is given", async () => {
  await under({ CLM_EDIT_RULE: "digest" }, `t${process.pid}n`, async (h, file) => {
    const raw = [user("Read the paper"), read("sed -n 1,80p paper.txt")];
    const first = await step(h, raw, "sed -n 1,80p paper.txt", "paper text ".repeat(300));
    const script = first.result.match(/python3 (\S+digest\.py) <<'NOTES'/)[1];
    const digest = `python3 ${script} <<'NOTES'\n- section 1 defines the mirror file\nNOTES`;
    raw.push(toolResult(first.result), read(digest));

    // The harness admits the digest command, the script edits the mirror, the edit applies.
    await h.hooks.context({ messages: raw }, h.ctx);
    await h.hooks.turn_start({}, h.ctx);
    const admitted = await h.hooks.tool_call({ toolName: "bash", toolCallId: "c1", input: { command: digest } }, h.ctx);
    assert.equal(admitted, undefined);
    const ran = spawnSync("python3", [script], { input: "- section 1 defines the mirror file\n", encoding: "utf8" });
    assert.equal(ran.status, 0, ran.stderr);
    assert.match(ran.stdout, /1 raw output\(s\) removed; your notes are this command/);
    const mirror = readFileSync(file, "utf8");
    assert.doesNotMatch(mirror, /paper text paper text/);
    assert.doesNotMatch(mirror, /role=tool/);
    const result = await h.hooks.tool_result(
      { toolName: "bash", content: [{ type: "text", text: ran.stdout }], isError: false }, h.ctx);
    assert.match(result.content[0].text, /edit applied/);
    assert.doesNotMatch(result.content[0].text, /DIGEST FIRST/);

    const empty = spawnSync("python3", [script], { input: "", encoding: "utf8" });
    assert.notEqual(empty.status, 0);
    assert.match(empty.stderr, /nothing was digested/);
    const template = spawnSync("python3", [script],
      { input: "<replace this line with your own notes on that output>\n", encoding: "utf8" });
    assert.notEqual(template.status, 0);
    assert.match(template.stderr, /Put your own notes/);
  });
});

test("short outputs gather until together they are due; the model's own printed words are not output", async () => {
  await under({ CLM_EDIT_RULE: "digest" }, `t${process.pid}q`, async (h) => {
    const table = "0.5x 2x 8x 24x ".repeat(80); // ~300 tokens: under the size alone
    const raw = [user("Read the paper"), read("sed -n 1,20p paper.txt")];
    const one = await step(h, raw, "sed -n 1,20p paper.txt", table);
    assert.doesNotMatch(one.result, /DIGEST FIRST/);
    raw.push(toolResult(one.result), read("sed -n 21,40p paper.txt"));
    const two = await step(h, raw, "sed -n 21,40p paper.txt", table);
    assert.match(two.result, /DIGEST FIRST: ~\d+ tokens/);
  });
  await under({ CLM_EDIT_RULE: "digest" }, `t${process.pid}r`, async (h) => {
    const answer = "Ten bullets: " + "the model edits its own context. ".repeat(80);
    const said = `cat <<'EOF'\n${answer}\nEOF`;
    const raw = [user("Summarise"), read(said)];
    const spoken = await step(h, raw, said, answer);
    assert.doesNotMatch(spoken.result, /DIGEST FIRST/);
  });
});

// ------------------------------------------- what an edit must prove before it is applied

test("literals are the strings an answer commits to, never a word of letters alone", () => {
  assert.deepEqual(literals("The port is 8080 in services/billing/settings.conf:3"),
    ["8080", "services/billing/settings.conf", "3"]);
  assert.deepEqual(literals("`BULK_FROM` reads e.g. orders.py, so 30s isn't 30"),
    ["BULK_FROM", "orders.py", "30s", "30"]);
  assert.deepEqual(literals("and/or hello world e.g."), []);
});

test("a literal is held whole, and a JSON screen is read as its rows", () => {
  const shown = Seen.of(['{"screen": "row one\\nrow two at 49152", "title": "BULK_FROM"}',
                         "plain text with orders.py"]);
  assert.ok(shown.holds("49152"));
  assert.ok(!shown.holds("4821"));
  assert.ok(shown.holds("row two"));
  assert.ok(!shown.holds("rowtwo"));
  assert.ok(shown.holds("BULK_FROM"));
  assert.ok(!Seen.of(["E4821"]).holds("E48"));
});

test("an answer may not cite a literal its own compaction removed", () => {
  const active = Seen.of(["SYSTEM", "the task"]);
  const evidence = new Set(literals("settings.conf says port 49152 and host E4821"));
  assert.deepEqual(citedEvidenceMissing("The port is 49152 on E4821", active, evidence),
    ["49152", "E4821"]);
  assert.deepEqual(citedEvidenceMissing("The task says E4821", Seen.of(["SYSTEM", "the task E4821"]), evidence), []);
  assert.deepEqual(citedEvidenceMissing("the value is 777", active, evidence), []);
});

test("the acceptance checks name each proof an edit broke, and nothing else", () => {
  const long = "one long instruction line ".repeat(4);
  const before = `parser.py:142 drops quoted commas\n${long}\n[[CTX_TURN 1 role=assistant]]\na`;
  assert.deepEqual(acceptanceProblems(before, before), []);
  assert.deepEqual(addedRepeats(before, `${before}\n${long}`), [{ line: long.trim(), count: 2 }]);
  assert.deepEqual(repeatedTurnIndices(before), []);
  assert.deepEqual(repeatedTurnIndices(`${before}\n[[CTX_TURN 1 role=tool]]\nb`), [1]);
  const broken = acceptanceProblems(before,
    `grep done\n${long}\n${long}\n[[CTX_TURN 1 role=assistant]]\na\n[[CTX_TURN 1 role=tool]]\nb`);
  assert.equal(broken.length, 2);
  assert.match(broken[0], /repeated 1 long line\(s\) instead of replacing them/);
  assert.match(broken[1], /wrote turn 1 twice/);
});

test("a final answer citing a literal the compaction dropped is refused, and passes once restored", async () => {
  const session = `t${process.pid}f`;
  const h = harness(session);
  await h.hooks.session_start({}, h.ctx);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const output = "settings.conf says port 49152 and host E4821";
  const raw = [user("Find the port"),
    assistant([{ type: "toolCall", id: "c0", name: "bash", arguments: { command: "cat settings.conf" } }]),
    toolResult(output)];
  // Pi persists every message; the check reads what a tool result ever showed.
  h.branch.push({ type: "message", message: raw[2] });
  await h.hooks.context({ messages: raw }, h.ctx);
  await h.hooks.turn_start({}, h.ctx);
  await h.hooks.tool_call({ toolName: "bash", toolCallId: "c1", input: { command: `sed -i x ${file}` } }, h.ctx);
  writeFileSync(file, readFileSync(file, "utf8").replace(output, "[port checked]"));
  const after = await h.hooks.tool_result(
    { toolName: "bash", content: [{ type: "text", text: "" }], isError: false }, h.ctx);
  assert.match(after.content[0].text, /edit applied/);
  // The next request is the one the answer comes from: it holds the edit, not the output.
  await h.hooks.context({ messages: raw }, h.ctx);

  const answer = assistant([{ type: "text", text: "The port is 49152 on E4821" }], { stopReason: "stop" });
  const refused = await h.hooks.message_end({ message: answer }, h.ctx);
  assert.equal(refused.message.stopReason, "error");
  assert.match(refused.message.errorMessage, /cites removed-only literal\(s\): "49152", "E4821"/);

  const restored = (await h.hooks.context({ messages: [...raw,
    toolResult("re-read: host E4821, port 49152")] }, h.ctx)).messages;
  assert.ok(restored.some((message) => JSON.stringify(message.content).includes("49152")));
  const passed = await h.hooks.message_end({ message: answer }, h.ctx);
  assert.equal(passed, undefined);
});

test("a compaction that copies a long line instead of replacing it is refused", async () => {
  const session = `t${process.pid}g`;
  const h = harness(session);
  await h.hooks.session_start({}, h.ctx);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const frame = "Your conversation is mirrored to the live context file, which you edit to free budget. ".repeat(2);
  const raw = [user("TASK"), assistant([{ type: "text", text: frame }, { type: "toolCall", id: "c0", name: "bash", arguments: { command: "cat big" } }]),
               toolResult("x".repeat(3000))];
  const { result } = await turn(h, raw, `sed -i x ${file}`, (text) => `${text}\n${frame.trim()}`);
  assert.match(result, /edit REJECTED — it broke a compaction rule: it repeated 1 long line\(s\)/);
  assert.ok(!h.branch.some((entry) => entry.data?.kind === "edit"));
});

test("an edit that writes one turn index twice is refused", async () => {
  const session = `t${process.pid}h`;
  const h = harness(session);
  await h.hooks.session_start({}, h.ctx);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const raw = [user("TASK"), assistant([{ type: "text", text: "a" }, { type: "toolCall", id: "c0", name: "bash", arguments: { command: "cat big" } }]),
               toolResult("x".repeat(3000))];
  const { result } = await turn(h, raw, `sed -i x ${file}`,
    () => "[[CTX_TURN 1 role=assistant]]\na\n\n[[CTX_TURN 1 role=tool]]\nb");
  assert.match(result, /edit REJECTED — it broke a compaction rule: it wrote turn 1 twice/);
  assert.ok(!h.branch.some((entry) => entry.data?.kind === "edit"));
});
