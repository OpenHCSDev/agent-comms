// The native CLM extension against the reference harness it ports
// (facebookresearch/context-language-models clm/clm_harness at 3c89408). GOLDEN is that
// harness's own output on the same inputs: render_editable, parse_back and the budget
// controller's messages, so any drift from what the checkpoint was trained on fails here.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import clmReference, {
  BudgetController, normalizedTurns, renderEditable,
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
  const prompt = await h.hooks.before_agent_start({ systemPrompt: "pi default" }, h.ctx);
  assert.match(prompt.systemPrompt, /mirrored to `\/tmp\/\.live_ctx\/.+\/LIVE_CTX_MAIN\.txt`/);
  assert.doesNotMatch(prompt.systemPrompt, /pi default/);
  const file = `/tmp/.live_ctx/${session}/LIVE_CTX_MAIN.txt`;
  const raw = [user("TASK"), assistant([{ type: "toolCall", id: "c0", name: "bash", arguments: { command: "cat big" } }]),
               toolResult("x".repeat(4000) + "\n\n(exit_code=0)")];
  const call = assistant([{ type: "toolCall", id: "c1", name: "bash", arguments: { command: `python3 edit ${file}` } }]);
  raw.push(call);
  const edited = await turn(h, raw.slice(0, 3), `python3 edit ${file}`,
    () => "[[CTX_TURN 1 role=assistant]]\nran cat big\n\n[[CTX_TURN 2 role=tool]]\n[big output: 4000 x]");
  assert.match(edited.input.command, /_dr_ec=\$\?/);
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
