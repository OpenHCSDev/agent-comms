// The Context Language Models reference harness (facebookresearch/context-language-models,
// clm/clm_harness at 3c89408) as a native Pi extension, for checkpoints RL-trained under it
// (context-language-model/rl-ckpt-livectx_dapo-iter44). Every string the model reads, the
// mirror format, the edit gate and the budget state machine are ported verbatim from
// clm_agent/prompts.yaml, context_utils/context_string.py, context_env/env.py,
// context_env/edit_gate.py and utils/budget.py; a checkpoint is served under the protocol it
// learned, not a lookalike.
//
// Pi owns the raw session (append-only). This extension owns the LIVE view the model is sent:
// the protected task prefix, then the last applied edit of the editable region, then the raw
// turns since that edit, with the harness's own notices inserted and rolled-back turns left
// out. Each of those is one custom session entry, so a restart rebuilds the same view.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const ENTRY = "clm-reference";
const CTX_ROOT = "/tmp/.live_ctx";
const CTX_NAME = "LIVE_CTX_MAIN.txt";
const VIEW_NAME = "LIVE_VIEW.json";
const OUTPUT_ROOT = "/tmp/.clm_outputs";
const DIGEST_NAME = "digest.py";
/** The digest command, written beside each conversation's mirror: it replaces the largest raw
 * command output in the mirror with the notes on its standard input. The model supplies only
 * the notes. Writing the edit itself asked too much of iter44 on the second part of a paper
 * (CLM-read, 2026-11-20: one good digest, then ten refused commands and no edit). */
export const DIGEST_PLACEHOLDER = "<replace this line with your own notes on that output>";
export const DIGEST_SCRIPT = String.raw`import os, re, sys
mirror = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LIVE_CTX_MAIN.txt")
notes = sys.stdin.read().strip()
if not notes or "<replace this line" in notes:
    sys.exit("digest: nothing was digested. Put your own notes on the output between the NOTES lines.")
text = open(mirror, encoding="utf-8").read()
headers = list(re.finditer(r"^\[\[CTX_TURN\s+\d+\s+role=([A-Za-z]+)\]\]\s*$", text, re.M))
spans = [(headers[i].start(), headers[i + 1].start() if i + 1 < len(headers) else len(text))
         for i in range(len(headers)) if headers[i].group(1).lower() == "tool"]
if not spans:
    sys.exit("digest: no command output is left in your context")
# Every raw output goes. The notes are not copied here: the command that carries them is the
# model's own next turn, and a second copy would cost their size again at every digest.
for start, end in reversed(spans):
    text = text[:start] + text[end:]
open(mirror, "w", encoding="utf-8").write(text)
print("digested: %d raw output(s) removed; your notes are this command" % len(spans))
`;
const PROTECT = 1; // Pi sends the system prompt separately: the protected prefix is the task.
const TRIVIAL_EDIT_TOKENS = 32;
const ROLLBACK_ESCALATE_AFTER = 3;
const ADAPTIVE_OBS_WINDOW = 3;
const ADAPTIVE_FLOOR = 0.5;
const ADAPTIVE_MIN_BAND = 0.1;
const LEDGER = -2; // the rollback ledger's origin: pinned, never merged into a turn

// ----------------------------------------------------------------- prompts (prompts.yaml)

export function systemPrompt(budgetText, ctxFile) {
  return `You are a helpful assistant that can interact with a computer.

Your response must include a THOUGHT section before your action where you
explain your reasoning. After the THOUGHT, you must call the \`bash\` tool
with EXACTLY ONE bash command (multiple commands chained with \`&&\` or \`||\`
count as a single action).

Failure to follow these rules — calling no tool, calling a tool other than
\`bash\`, or omitting the THOUGHT — will cause your response to be rejected.

## Managing your context

**Goal: maximize task success** — keep going until the task is done or you run out of
budget (no penalty for extra turns). Your context budget is ${budgetText}; each
result shows your current size.

Your conversation is mirrored to \`${ctxFile}\` (refreshed before
every command). **Free up context by editing that file** — replace stale regions (big
outputs, dead ends, superseded notes) with a concise, specific summary. A bloated
transcript wastes budget and dulls your reasoning, so compact as it grows. An editing
turn is free only when its command applies a real edit and prints nothing — any
stdout/stderr, a non-zero exit, or an edit that matches nothing costs a normal turn.

**Locate text with code — never paste or retype it** (context is long: retyping wastes
tokens and usually mis-matches). Turns are numbered \`[[CTX_TURN 1 …]]\`, \`[[CTX_TURN 2 …]]\`,
… in order from the top of the editable region (the hidden system/task prefix is NOT
counted, so the first turn you can edit is 1). Match a turn by its \`[[CTX_TURN <i> role=…]]\`
header, a block by a short unique first/last line, or slice on the headers:
    python3 - <<'PY'
    import re; p="${ctxFile}"; s=open(p).read()
    # collapse turn 7 (its header -> the next header); its body is never retyped:
    s=re.sub(r"(\\[\\[CTX_TURN 7 [^\\]]*\\]\\]).*?(?=\\n\\[\\[CTX_TURN|\\Z)",
             r"\\1\\n[grep done: parser.py:142 drops quoted commas; fix=csv.reader]", s, flags=re.S)
    open(p,"w").write(s)
    PY

**Rules:** don't \`cat\` this file (its text is already in your context). Keep the
\`[[CTX_TURN …]]\` header of any turn you keep — emptying its text drops that turn; the
system/task prefix is protected for you. Each result says whether the file changed or
matched nothing.

**What a compaction must keep.** An edit is refused and not applied when it drops a fact or
copies text instead of replacing it:
- Mark a line you may not lose with \`KEEP:\` at its start. Every marked line must survive the
  edit; the request you are working on is kept for you.
- Never repeat a long line to preserve it — that is a copy, not a summary, and the edit is
  refused. Write the fact once in your own words.
- Give every turn one index. Writing the same \`[[CTX_TURN i]]\` twice makes the turn you just
  wrote unfindable by the address you will use.

**Compact cheaply** — an edit forces everything *after* it to be re-read, so cost grows
with how much text FOLLOWS the edit:
- **Batch**: one large compaction beats many small edits.
- **Mind what's below your edit** — it all gets re-read, so don't compact a small early
  region while a long, still-useful tail sits beneath it (that re-reads the whole tail
  for little gain). Keep a useful tail; if it's much larger than what you'd compact,
  wait and compact head + tail together — UNLESS you expect to hit the context limit
  soon, then compact now.
- **Be generous in the summary**: the tail is re-read regardless, so a detailed
  replacement is essentially free.

${FINISH}`;
}

// The reference's open_ended policy has no finish paragraph; a bus input ends the way Pi
// ends a turn, with a reply that calls no tool. That reply is delivered as the answer.
const FINISH = "When you are finished, give your final answer in a reply that calls no tool.";

const TOO_LONG_HINT = `The output of your last command was too long. Try a command that produces
less output (head/tail/sed for fewer lines, a more selective grep/find, or
redirect to a file and search it).
`;

// --------------------------------------------- context as a string (context_string.py)

const HEADER_RE = /^\[\[CTX_TURN\s+(\d+)\s+role=([A-Za-z]+)\]\]\s*$/gm;
const VALID_ROLES = new Set(["system", "user", "assistant", "tool"]);

export function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  const parts = [];
  for (const part of content) {
    if (typeof part === "string") parts.push(part);
    else if (part && (part.type === undefined || part.type === "text") && typeof part.text === "string") {
      parts.push(part.text);
    } else if (part && (part.type === "thinking" || part.type === "toolCall")) {
      continue; // rendered separately by renderedText
    } else return null;
  }
  return parts.join("");
}

/** The role a Pi message has in the reference's chat log. */
export function referenceRole(message) {
  if (message.role === "assistant") return "assistant";
  if (message.role === "toolResult") return "tool";
  return "user";
}

/** reasoning, then content, then the issued command (name + JSON arguments). */
export function renderedText(message) {
  const parts = [];
  const content = Array.isArray(message.content) ? message.content : [];
  for (const part of content) {
    if (part?.type === "thinking" && part.thinking) parts.push(part.thinking);
  }
  const text = contentText(message.content);
  if (text) parts.push(text);
  for (const part of content) {
    if (part?.type !== "toolCall") continue;
    const args = typeof part.arguments === "string" ? part.arguments : pythonJson(part.arguments ?? {});
    const piece = `${part.name ?? ""} ${args}`.trim();
    if (piece) parts.push(piece);
  }
  return parts.filter(Boolean).join("\n");
}

/** JSON as Python's json.dumps writes it (", " and ": " separators, ASCII escapes): the
 * reference renders a command's arguments that way, so the checkpoint learned that form. */
export function pythonJson(value) {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(", ")}]`;
  if (typeof value === "object") {
    return `{${Object.entries(value).map(([k, v]) => `${pythonJson(k)}: ${pythonJson(v)}`).join(", ")}}`;
  }
  if (typeof value === "string") {
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
      (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  }
  return JSON.stringify(value);
}

export function renderEditable(messages, protect) {
  const blocks = [];
  for (let i = protect, n = 1; i < messages.length; i++, n++) {
    blocks.push(`[[CTX_TURN ${n} role=${referenceRole(messages[i])}]]\n${renderedText(messages[i])}`);
  }
  return blocks.join("\n\n");
}

/** Sections of an edited mirror: [role, body] in document order, as parse_back reads them. */
export function parseSections(edited) {
  const text = typeof edited === "string" ? edited : String(edited);
  const matches = [...text.matchAll(HEADER_RE)];
  const sections = [];
  if (matches.length === 0) {
    const body = text.trim();
    if (body) sections.push(["user", body]);
    return sections;
  }
  const lead = text.slice(0, matches[0].index).trim();
  if (lead) sections.push(["user", lead]);
  matches.forEach((match, k) => {
    let role = match[2].toLowerCase();
    if (!VALID_ROLES.has(role)) role = "user";
    const end = k + 1 < matches.length ? matches[k + 1].index : text.length;
    const body = text.slice(match.index + match[0].length, end).trim();
    if (body) sections.push([role, body]);
  });
  return sections;
}

/** Sections as plain turns: assistant stays assistant, everything else becomes user; empty
 * turns dropped and consecutive same-role turns merged (_normalize). */
export function normalizedTurns(edited) {
  const merged = [];
  for (const [role, body] of parseSections(edited)) {
    const kind = role === "assistant" ? "assistant" : "user";
    const last = merged[merged.length - 1];
    if (last && last.role === kind) last.text = `${last.text}\n\n${body}`.trim();
    else merged.push({ role: kind, text: body });
  }
  return merged;
}

// ------------------------------------------------- what an edit must prove (acceptance)

/** A line the model may not lose starts with this. The prompt tells the model to mark a fact
 * this way instead of hoping some later compaction keeps it; the gate enforces the mark. A
 * compaction that only meant to shrink is refused when it drops one, so the model's own notes
 * survive it. */
export const KEEP_PREFIX = "KEEP:";

/** A line at this length repeated in the compacted text is a copy, not content. The frames and
 * instructions a transcript repeats were 25 copies of one 1,152-character block, 30.3% of the
 * context (CLM-tristan audit, 2026-11-20): the size-only gate kept every copy and the model
 * lost its own single-authored map. An edit may not raise any line's count. */
export const REPEAT_MIN_CHARS = 100;

function lineCounts(text) {
  const counts = new Map();
  for (const line of text.split("\n")) {
    const key = line.trim();
    if (key.length >= REPEAT_MIN_CHARS) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

/** The marked lines `before` holds and `edited` dropped. */
export function droppedKeeps(before, edited) {
  const kept = new Set(edited.split("\n").map((line) => line.trim()));
  return before.split("\n").map((line) => line.trim())
    .filter((line) => line.startsWith(KEEP_PREFIX) && !kept.has(line));
}

/** Long lines the edit copied: each one's count rose above what the mirror already held, so
 * the edit repeated text where a summary belongs. */
export function addedRepeats(before, edited) {
  const was = lineCounts(before);
  const copied = [];
  for (const [line, count] of lineCounts(edited)) {
    if (count > Math.max(was.get(line) ?? 0, 1)) copied.push({ line, count });
  }
  return copied;
}

/** Turn indices the edit wrote twice: the model targets a turn by its index, so a repeated
 * index makes the replacement it just wrote unfindable by the address it will use. */
export function repeatedTurnIndices(edited) {
  const seen = new Set();
  const repeated = [];
  for (const match of edited.matchAll(HEADER_RE)) {
    const index = Number(match[1]);
    if (seen.has(index) && !repeated.includes(index)) repeated.push(index);
    seen.add(index);
  }
  return repeated;
}

/** Every proof the edit owes and did not pay, in the order a reader needs them. */
export function acceptanceProblems(before, edited) {
  const problems = [];
  const dropped = droppedKeeps(before, edited);
  if (dropped.length) {
    const named = dropped.slice(0, 3).map((line) => JSON.stringify(line.slice(0, 60))).join(", ");
    problems.push(`it dropped ${dropped.length} line(s) you marked ${KEEP_PREFIX} (${named})`);
  }
  const copied = addedRepeats(before, edited);
  if (copied.length) {
    const named = copied.slice(0, 2)
      .map(({ line, count }) => `${JSON.stringify(line.slice(0, 40))} ×${count}`).join(", ");
    problems.push(`it repeated ${copied.length} long line(s) instead of replacing them (${named})`);
  }
  const repeated = repeatedTurnIndices(edited);
  if (repeated.length) problems.push(`it wrote turn ${repeated.map(String).join(", ")} twice`);
  return problems;
}

export function acceptanceNote(problems, before) {
  return (
    `\n[LIVE_CTX_MAIN.txt: edit REJECTED — it did not keep what a compaction must keep: ` +
    `${problems.join("; ")}, so it was NOT applied (still ~${before}). ` +
    `Keep every ${KEEP_PREFIX} line, replace text instead of repeating it, and give each turn ` +
    `one index.]`
  );
}

// ------------------------------------------------------------------- tokens and budget

/** Retained-context size: characters / 4 (the reference's no-encoder ruler), scaled by the
 * ratio learned from the server's reported prompt tokens. */
export function rawTokens(texts) {
  let chars = 0;
  for (const text of texts) chars += text.length;
  return Math.ceil(chars / 4);
}

export function messageTexts(messages) {
  return messages.map(renderedText);
}

export class BudgetController {
  constructor({ budget, reserve = 2048, nudgeRatios = [0.25, 0.5, 0.75], obsMult = 2.0,
                maxRetries = 50, retryMargin = 2048, compactionHint = "" }) {
    this.budget = budget;
    this.reserve = reserve;
    this.strictTarget = budget - reserve;
    this.nudgeRatios = [...nudgeRatios].sort((a, b) => a - b);
    this.obsMult = obsMult;
    this.maxRetries = maxRetries;
    this.retryMargin = retryMargin;
    this.compactionHint = compactionHint;
    this.tokRatio = 1.0;
    this.nudged = new Set();
    this.finalized = false;
    this.nRetry = 0;
    this.nTurnsRolledBack = 0;
    this.consecRollbacks = 0;
  }

  count(texts) {
    return Math.trunc(rawTokens(texts) * this.tokRatio);
  }

  calibrate(promptTokens, sentTexts) {
    const raw = rawTokens(sentTexts);
    if (!(promptTokens > 0) || raw <= 0) return;
    const ratio = promptTokens / raw;
    if (ratio >= 0.5 && ratio <= 3.0) this.tokRatio = ratio;
  }

  /** utils/budget.py BudgetController.evaluate: {action, tokens, message?}. `toolTexts` are the
   * retained tool outputs, newest last, for the adaptive urgent band. */
  evaluate(texts, toolTexts, droppable) {
    const tc = this.count(texts);
    this.nudged = new Set([...this.nudged].filter((f) => tc >= Math.trunc(this.budget * f)));
    if (tc > this.strictTarget) {
      if (this.nRetry < this.maxRetries && this.strictTarget > this.retryMargin && droppable) {
        this.nRetry += 1;
        this.consecRollbacks += 1;
        return { action: "rollback", tokens: tc };
      }
      if (this.finalized) return { action: "stop", tokens: tc };
      this.finalized = true;
      return { action: "final_turn", tokens: tc, message: this.finalMessage(tc) };
    }
    this.finalized = false;
    const [need] = this.adaptiveTrigger(toolTexts);
    if (this.strictTarget - tc < need) {
      const frac = (this.strictTarget - need) / this.strictTarget;
      return { action: "continue", tokens: tc, message: this.persistentNudgeMessage(tc, frac) };
    }
    const crossed = this.nudgeRatios.filter((f) => tc >= Math.trunc(this.budget * f));
    const toFire = crossed.filter((f) => !this.nudged.has(f));
    if (toFire.length) {
      const tier = Math.max(...toFire);
      for (const f of crossed) this.nudged.add(f);
      return { action: "continue", tokens: tc, message: this.nudgeMessage(tier, tc) };
    }
    return { action: "continue", tokens: tc };
  }

  adaptiveTrigger(toolTexts) {
    const limit = this.strictTarget;
    const obs = Math.max(0, ...toolTexts.slice(-ADAPTIVE_OBS_WINDOW).map((t) => this.count([t])));
    const byRatio = Math.trunc(ADAPTIVE_MIN_BAND * limit);
    const byObs = Math.trunc(this.obsMult * obs);
    const cap = Math.trunc((1.0 - ADAPTIVE_FLOOR) * limit);
    if (byObs <= byRatio) return [byRatio, obs, "ratio"];
    if (byObs > cap) return [cap, obs, "obs_capped"];
    return [byObs, obs, "obs"];
  }

  noteCompaction() {
    this.consecRollbacks = 0;
  }

  rollbackMargin() {
    const depth = Math.max(1, this.consecRollbacks - ROLLBACK_ESCALATE_AFTER);
    return Math.min(this.retryMargin * depth, Math.max(Math.trunc(3 * this.strictTarget / 4), this.retryMargin));
  }

  rollbackMessage(nDropped, tokensAfter, tried) {
    let body =
      `[SYSTEM NOTICE — CONTEXT LIMIT HIT (retry ${this.nRetry}/${this.maxRetries})] Your context crossed the ${this.budget}-token limit, ` +
      `so your ${nDropped} most recent turn(s) were ROLLED BACK and are gone — that work is ` +
      `no longer in your context and cannot be recovered (${this.nTurnsRolledBack} turn(s) ` +
      `lost so far). You are now at ~${tokensAfter}/${this.strictTarget} tokens, which leaves ` +
      "room for exactly one thing: CONDENSE YOUR CONTEXT THIS TURN and do nothing else. " +
      "Replace stale regions with short summaries.";
    if (this.compactionHint) body += " " + this.compactionHint;
    if (tried.length) {
      body +=
        "\nThese commands already ran and their output is what blew your budget, so their " +
        "turns were discarded. Re-running them will just lose the context again — if you " +
        "need one, make it print far less (head/grep/count instead of dumping):\n" +
        tried.map((c) => `  - ${c}`).join("\n");
    }
    return body;
  }

  finalMessage(tokens) {
    return (
      `[SYSTEM NOTICE] You have reached your context budget (${tokens}/${this.budget} ` +
      "tokens). This is your FINAL turn: make sure your solution is complete and in " +
      "place — write any required output/answer to the location the task specifies (or " +
      "run your submit command) — because after this turn the session ends and your " +
      "work is evaluated as-is."
    );
  }

  persistentNudgeMessage(tokens, ratio) {
    let body =
      `You are at ${tokens}/${this.strictTarget} tokens — over ` +
      `${Math.round(ratio * 100)}% of your hard context limit and about ` +
      "to be cut off. Compact your context THIS TURN (do nothing else): remove stale regions " +
      "now. If you cross the limit you get exactly one final turn and then the session ends " +
      "with your work graded as-is.";
    if (this.compactionHint) body += " " + this.compactionHint;
    return "CONTEXT BUDGET NUDGE (URGENT): " + body;
  }

  nudgeMessage(frac, tokens) {
    const NOTE =
      "When you write a replacement note, COPY facts forward from the text you are " +
      "replacing (quote them): every RULED OUT candidate with its reason and the words " +
      "'do not retry'; the exact queries/commands already tried; exact values marked " +
      "VERIFIED or UNVERIFIED; and a NEXT line.";
    const pct = Math.round(frac * 100);
    if (frac <= 0.25) {
      return (
        "CONTEXT BUDGET NUDGE: " +
        `context is at ~${pct}% of your ${this.budget}-token budget (${tokens} tokens). ` +
        "No action needed. Before your next few searches, make sure your notes " +
        "record which queries and documents you already tried."
      );
    }
    let body;
    if (frac <= 0.5) {
      body =
        `context is at ~${pct}% of your ${this.budget}-token budget (${tokens} tokens). ` +
        "Finish the unit of work in flight, then tidy ONCE. " + NOTE;
    } else {
      body =
        `context is at ~${pct}% of your ${this.budget}-token budget (${tokens} tokens) — ` +
        "close to the limit. Compact settled spans now — but do NOT wipe: edits " +
        "keeping under 25% of the region they touch are usually followed by " +
        "re-doing the deleted work. " + NOTE +
        " if you cross the limit you get one final turn and then the session ends.";
    }
    if (this.compactionHint) body += " " + this.compactionHint;
    return "CONTEXT BUDGET NUDGE: " + body;
  }
}

// ------------------------------------------------------------------- observations (env.py)

export function truncateObservation(output, maxChars = 10000, head = 5000, tail = 5000) {
  if (output.length <= maxChars) return output;
  if (head + tail > maxChars) head = tail = Math.trunc(maxChars / 2);
  const elided = output.length - head - tail;
  return (
    `${TOO_LONG_HINT}\n\n---- HEAD (${head} chars) ----\n${output.slice(0, head)}\n` +
    `---- ${elided} chars elided ----\n` +
    `---- TAIL (${tail} chars) ----\n${output.slice(-tail)}`
  );
}

/** Pi's bash result as the reference formats it: output, then (exit_code=N). */
export function formatToolResult(text, isError) {
  let output = text;
  let code = 0;
  const exited = /\n*Command exited with code (\d+)\s*$/.exec(output);
  if (exited) {
    code = Number(exited[1]);
    output = output.slice(0, exited.index);
  } else if (isError) {
    code = 1;
  }
  output = output.trimEnd();
  const shown = output ? truncateObservation(output) : "(no output)";
  return { block: `${shown}\n\n(exit_code=${code})`, code, empty: !output };
}

export function rejectedNote(baseline, after, before, rule) {
  return (
    `\n[LIVE_CTX_MAIN.txt: edit REJECTED — it GREW context ` +
    `~${baseline}->${after} tokens, so it was NOT applied (still ~${before}). ` +
    `${rule}: you likely duplicated/appended ` +
    `content — replace stale text with a SHORTER summary instead.]`
  );
}

// ---------------------------------------------------------------------- the live view

/** The live context over Pi's raw messages, rebuilt from this extension's entries. */
export class LiveView {
  constructor() {
    this.base = null; // {covers, text}: an applied edit of raw[PROTECT, covers)
    this.inserts = []; // {at, content}: harness notices inserted before raw[at]
    this.dropped = []; // {from, to}: raw ranges rolled back
    this.ledger = null; // the rollback ledger, pinned after the task
  }

  apply(data) {
    if (data.kind === "edit") {
      this.base = { covers: data.covers, text: data.text };
      this.inserts = this.inserts.filter((insert) => insert.at >= data.covers);
      this.dropped = this.dropped.filter((range) => range.to > data.covers)
        .map((range) => ({ from: Math.max(range.from, data.covers), to: range.to }));
    } else if (data.kind === "insert") {
      this.inserts.push({ at: data.at, content: data.content });
    } else if (data.kind === "rollback") {
      this.dropped.push({ from: data.from, to: data.to });
      this.inserts = this.inserts.filter((insert) => insert.at <= data.from);
      this.ledger = data.ledger;
    }
  }

  /** A rolled-back raw turn. An input is never one: inputs are the task, which the reference
   * protects, and Pi refuses a request that has lost an input it is delivering. */
  isDropped(index, message) {
    if (message?.role === "user" && message.inputId !== undefined) return false;
    return this.dropped.some((range) => index >= range.from && index < range.to);
  }

  /** [messages, rawIndexOf] for the request: rawIndexOf[i] is the raw index of messages[i],
   * or -1 for a message this view made (the ledger, an edited turn, a notice). */
  build(raw, template) {
    const out = [];
    const origin = [];
    for (let i = 0; i < Math.min(PROTECT, raw.length); i++) {
      out.push(raw[i]);
      origin.push(i);
    }
    if (this.ledger) {
      out.push(userMessage(this.ledger));
      origin.push(LEDGER);
    }
    let start = PROTECT;
    if (this.base && raw.length >= this.base.covers) {
      for (const turn of normalizedTurns(this.base.text)) {
        out.push(turn.role === "assistant" ? assistantMessage(turn.text, template) : userMessage(turn.text));
        origin.push(-1);
      }
      start = this.base.covers;
    }
    for (let i = start; i <= raw.length; i++) {
      for (const insert of this.inserts) {
        if (insert.at === i) {
          out.push(userMessage(insert.content));
          origin.push(-1);
        }
      }
      if (i < raw.length && !this.isDropped(i, raw[i])) {
        out.push(raw[i]);
        origin.push(i);
      }
    }
    return [mergeAdjacentUsers(out, origin), origin];
  }
}

function userMessage(text) {
  return { role: "user", content: [{ type: "text", text }], timestamp: 0 };
}

function assistantMessage(text, template) {
  return {
    role: "assistant",
    content: [{ type: "text", text }],
    api: template?.api,
    provider: template?.provider,
    model: template?.model,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
             cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "stop",
    timestamp: 0,
  };
}

/** Consecutive same-role turns merge, as _normalize does; only view-made user turns merge,
 * so a raw message is never rewritten. */
function mergeAdjacentUsers(messages, origin) {
  const out = [];
  const kept = [];
  messages.forEach((message, i) => {
    const last = out[out.length - 1];
    if (last && origin[i] === -1 && kept[kept.length - 1] === -1 &&
        message.role === "user" && last.role === "user") {
      out[out.length - 1] = userMessage(`${contentText(last.content)}\n\n${contentText(message.content)}`.trim());
      return;
    }
    out.push(message);
    kept.push(origin[i]);
  });
  origin.length = 0;
  origin.push(...kept);
  return out;
}

// ------------------------------------------------------------------------ the extension

/** The assistant message whose command is running: the newest message entry on the branch. */
function pendingAssistant(ctx) {
  const branch = ctx.sessionManager.getBranch?.() ?? [];
  for (let i = branch.length - 1; i >= 0; i--) {
    const message = branch[i].type === "message" ? branch[i].message : null;
    if (message) return message.role === "assistant" ? message : null;
  }
  return null;
}

/** The request the model is working on now: the newest input among the turns an edit covers.
 * The reference protects one task, the conversation's first message, because there one
 * conversation is one task. A thread takes many inputs, and each later one was an ordinary
 * editable turn: a compaction could delete it, leaving only the first task pinned, which the
 * model then answered again (CLM-ref2, 2026-11-20: asked to read a paper, it compacted and
 * returned the sum from its first task). An edit may reword anything else; the current
 * request is restored when an edit drops it. */
export class CurrentTask {
  static among(raw, covers) {
    for (let index = Math.min(covers, raw.length) - 1; index >= PROTECT; index--) {
      const message = raw[index];
      if (message?.role === "user" && message.inputId !== undefined) return new CurrentTask(renderedText(message));
    }
    return new NoCurrentTask();
  }

  constructor(text) {
    this.text = text;
  }

  /** `edited` with the task still in it; `restored` says whether it had to be put back. */
  keptIn(edited) {
    if (!this.text.trim() || edited.includes(this.text.trim())) return { text: edited, restored: false };
    return { text: `${edited.trimEnd()}\n\n[[CTX_TURN 0 role=user]]\n${this.text}`, restored: true };
  }
}

/** The covered turns hold no input after the protected first one: nothing to keep. */
export class NoCurrentTask extends CurrentTask {
  constructor() {
    super("");
  }
}

/** When the model must compact its context, beyond what the budget presses for. The
 * provider names the rule its models run under (CLM_EDIT_RULE); each member is one rule. */
export class EditRule {
  static members = new Map();

  static declare(name, member) {
    EditRule.members.set(name, member);
    return member;
  }

  static named(name) {
    const member = EditRule.members.get(name);
    if (!member) throw new Error(`Unknown CLM edit rule: ${JSON.stringify(name)}`);
    return new member();
  }

  /** Why `command` may not run now, or null when it may. */
  refusal(_command, _ctxDir) { return null; }

  /** A command's output of `tokens` entered the context. */
  observed(_tokens) {}

  /** An applied edit freed `tokens` of context. */
  compacted(_tokens) {}

  /** What the model is told after a command, or "". */
  reminder(_ctxDir) { return ""; }
}

/** The reference harness: only the budget's reminders and rollbacks press. */
export class BudgetPressure extends EditRule {}
EditRule.declare("budget", BudgetPressure);

/** Each sizeable output is digested before the next command runs: turned into notes and
 * dropped. iter44 compacts only near its limit (CLM-tristan, 2026-11-20: two edits in sixteen
 * turns, both after a budget reminder), which cannot read a document larger than its window;
 * here each part's digestion is the precondition of fetching the next. */
export class DigestEachOutput extends BudgetPressure {
  /** Raw output may wait until this much of it has gathered: one long output, or several
   * short ones (CLM-read2 left five tables of numbers raw, each under the size alone). */
  threshold = 500;
  /** Tokens of output brought in since the last compaction that removed them. */
  pending = 0;

  get due() {
    return this.pending >= this.threshold;
  }

  /** What to run, in words a small model can follow to the letter. */
  told(ctxDir) {
    return `~${this.pending} tokens of command output are still raw in your context and must be ` +
      "digested before anything else runs. Run exactly this, with your own notes between the NOTES lines:\n" +
      `python3 ${join(ctxDir, DIGEST_NAME)} <<'NOTES'\n${DIGEST_PLACEHOLDER}\nNOTES`;
  }

  refusal(command, ctxDir) {
    return this.due && !command.includes(ctxDir)
      ? `COMMAND NOT RUN. Nothing is wrong with your command or the file: ${this.told(ctxDir)}\nThen run your command again.`
      : null;
  }

  observed(tokens) {
    this.pending += tokens;
  }

  /** Digested once an edit removed at least half of what the outputs brought in: a rewrite
   * that keeps the raw text, or writes the same text back, frees nothing and does not count. */
  compacted(tokens) {
    if (tokens >= this.pending / 2) this.pending = 0;
  }

  reminder(ctxDir) {
    return this.due ? `\n[DIGEST FIRST: ${this.told(ctxDir)}]` : "";
  }
}
EditRule.declare("digest", DigestEachOutput);

/** Whether a model run may finish without applying an edit to the live context. This is
 * independent of EditRule: one governs when raw tool output must be digested, the other
 * governs what constitutes a completed user turn. */
export class ContextEditPolicy {
  static members = new Map();

  static declare(name, member) {
    ContextEditPolicy.members.set(name, member);
    return member;
  }

  static named(name) {
    const member = ContextEditPolicy.members.get(name);
    if (!member) throw new Error(`Unknown CLM context-edit policy: ${JSON.stringify(name)}`);
    return new member();
  }

  instruction(_ctxFile) { return ""; }
  started(_acceptedEdits) {}
  refusedTerminal(_message, _acceptedEdits) { return null; }
}

export class OptionalContextEdit extends ContextEditPolicy {}
ContextEditPolicy.declare("optional", OptionalContextEdit);

/** The run begins with one edit revision and may emit its final answer only after a later
 * accepted edit. A no-op, malformed or rejected mirror write never advances that revision. */
export class RequiredContextEdit extends ContextEditPolicy {
  baseline = 0;

  instruction(ctxFile) {
    return `\n\nBefore your final answer, you MUST apply one accepted edit to ${ctxFile} during this ` +
      "agent run. Replace settled detail with concise durable notes while preserving the current " +
      "request and every unresolved fact. A no-op or rejected edit does not count; the run will " +
      "fail instead of pretending its context was maintained.";
  }

  started(acceptedEdits) {
    this.baseline = acceptedEdits;
  }

  refusedTerminal(message, acceptedEdits) {
    if (message?.role !== "assistant" || message.stopReason !== "stop" || acceptedEdits > this.baseline) {
      return null;
    }
    return {
      ...message,
      stopReason: "error",
      errorMessage: "Context edit required: this turn ended without an accepted LIVE_CTX_MAIN.txt edit.",
    };
  }
}
ContextEditPolicy.declare("required_each_turn", RequiredContextEdit);

/** A command's output on its way into the context: shown when it fits the room left under
 * the limit, stored to a file and described when it does not. */
export class Observation {
  /** An output this small is always shown: storing it would cost more than it holds. */
  static small = 256;

  static entering({ block, code }, full, room, limit, count, store) {
    const tokens = count(block);
    return tokens <= Math.max(room, Observation.small)
      ? new ShownObservation(block, tokens)
      : StoredObservation.saving(full, code, limit - room, limit, count, store);
  }

  /** `text` is what enters the context, `tokens` its size there. */
  constructor(text, tokens) {
    this.text = text;
    this.tokens = tokens;
  }
}

export class ShownObservation extends Observation {}

/** An output that would have put the context over its limit. Ending the turn on an error
 * loses the work that produced it; the model is told its size and where it is instead, and
 * reads it in parts that fit. */
export class StoredObservation extends Observation {
  static saving(full, code, held, limit, count, store) {
    const file = store.save(full);
    const tokens = count(full);
    const lines = full.split("\n").length;
    const text = `[OUTPUT STORED, NOT SHOWN: this command's output is ~${tokens} tokens (${lines} lines); ` +
      `with ~${held} of your ${limit} tokens already in use it would not fit. It is saved in ${file}. ` +
      `Read it in parts that fit, for example: sed -n '1,60p' ${file} — and note what you need ` +
      `from each part before reading the next.]\n\n(exit_code=${code})`;
    return new StoredObservation(text, count(text));
  }
}

/** Stored outputs of one conversation: numbered files outside the context mirror's directory,
 * so reading one is never mistaken for an edit of the context. */
export class OutputStore {
  constructor(directory) {
    this.directory = directory;
    this.saved = 0;
  }

  save(text) {
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    this.saved += 1;
    const file = join(this.directory, `output-${String(this.saved).padStart(3, "0")}.txt`);
    writeFileSync(file, text, { encoding: "utf8", mode: 0o600 });
    return file;
  }
}

export default function clmReference(pi) {
  const budgetTokens = Number(process.env.CLM_CONTEXT_BUDGET || 28672);
  const reserve = Number(process.env.CLM_CONTEXT_RESERVE || 2048);
  const rule = EditRule.named(process.env.CLM_EDIT_RULE || "budget");
  const contextEdit = ContextEditPolicy.named(process.env.CLM_CONTEXT_EDIT_POLICY || "optional");
  let outputs;
  const hint =
    `Compact ${CTX_ROOT}/<session>/${CTX_NAME} by locating stale regions with code (match a turn by its ` +
    "[[CTX_TURN i ...]] header or a block by short start/end anchors) and replacing " +
    "them with summaries — do not retype the text you remove. Compact settled spans; " +
    "keep anything you have not finished using.";
  let budget;
  let view;
  let ctxDir;
  let ctxFile;
  let rolledBackCmds = [];
  // The request most recently sent, and the mirror written for the command it produced.
  let sent = { raw: [], messages: [], origin: [] };
  let mirror = null; // {rendered, covers, command}
  // The request's tool schemas, as the last provider payload carried them: counted with the
  // messages, so the estimate covers what is sent. The reference's one bash schema is a
  // rounding error; a fixed overhead of several thousand tokens is not, and a multiplicative
  // calibration cannot absorb it (CLM-ref, 2026-11-20: ratio drifted 2.6 -> 1.2).
  let toolsText = "";
  const turnCalls = new Set();
  let acceptedEdits = 0;

  function persist(data) {
    pi.appendEntry(ENTRY, data);
    view.apply(data);
    if (data.kind === "edit") acceptedEdits += 1;
  }

  function restore(ctx) {
    budget = new BudgetController({
      budget: budgetTokens, reserve, compactionHint: hint.replace(`${CTX_ROOT}/<session>/${CTX_NAME}`, ctxFile),
    });
    view = new LiveView();
    acceptedEdits = 0;
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type === "custom" && entry.customType === ENTRY && entry.data) {
        view.apply(entry.data);
        if (entry.data.kind === "edit") acceptedEdits += 1;
      }
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    const session = String(ctx.sessionManager.getSessionId()).replace(/[^A-Za-z0-9_-]/g, "_");
    ctxDir = join(CTX_ROOT, session);
    ctxFile = join(ctxDir, CTX_NAME);
    mkdirSync(ctxDir, { recursive: true, mode: 0o700 });
    writeFileSync(join(ctxDir, DIGEST_NAME), DIGEST_SCRIPT, { encoding: "utf8", mode: 0o700 });
    outputs = new OutputStore(join(OUTPUT_ROOT, session));
    restore(ctx);
  });

  pi.on("session_tree", async (_event, ctx) => restore(ctx));

  // The reference's system prompt is its template alone. Anything Pi would add, the fleet's
  // dealt rules included (123,111 tokens for a 49,152-token window on 2026-11-20), is
  // context the checkpoint never saw and cannot hold.
  pi.on("before_agent_start", async () => ({
    systemPrompt: systemPrompt(`${budget.strictTarget} tokens`, ctxFile) + contextEdit.instruction(ctxFile),
  }));

  pi.on("agent_start", () => contextEdit.started(acceptedEdits));

  pi.on("message_end", (event) => {
    const replacement = contextEdit.refusedTerminal(event.message, acceptedEdits);
    return replacement ? { message: replacement } : undefined;
  });

  pi.on("context", async (event, ctx) => {
    const raw = event.messages;
    const template = [...raw].reverse().find((message) => message.role === "assistant");
    // Calibrate on the request whose answer is now the newest assistant message.
    const promptTokens = (template?.usage?.input ?? 0) + (template?.usage?.cacheRead ?? 0);
    if (promptTokens > 0 && sent.messages.length) {
      budget.calibrate(promptTokens, [ctx.getSystemPrompt?.() ?? "", toolsText, ...messageTexts(sent.messages)]);
    }
    let [messages, origin] = view.build(raw, template);
    const system = ctx.getSystemPrompt?.() ?? "";
    const tools = messages.filter((message) => message.role === "toolResult").map(renderedText);
    const droppable = origin.some((index) => index >= PROTECT);
    const decision = budget.evaluate([system, toolsText, ...messageTexts(messages)], tools, droppable);
    if (decision.action === "rollback") {
      [messages, origin] = rollback(raw, template, system);
    } else if (decision.action === "stop") {
      ctx.abort();
    } else if (decision.message) {
      persist({ kind: "insert", at: raw.length, content: decision.message });
      [messages, origin] = view.build(raw, template);
    }
    sent = { raw, messages, origin };
    publishView(messages, system);
    return { messages };
  });

  /** The live view as sent, for the thread's owner to record: one atomic file holding the
   * system prompt, every turn in order with its reference role and text, and the budget's
   * state. The owner checkpoints it into the thread's context history; this extension only
   * publishes what it sent, it keeps no second copy of the context. */
  function publishView(messages, system) {
    const view = {
      version: 1,
      system,
      turns: messages.map((message) => ({ role: referenceRole(message), text: renderedText(message) })),
      // The size of the context this view sends, in the budget's own estimate: the owner is
      // the one that knows how big the context it holds is, and the protocol compares that
      // size between turns instead of deriving an estimate of its own.
      budget: { strict_target: budget.strictTarget, token_ratio: budget.tokRatio,
                tokens: budget.count(messageTexts(messages)),
                retries: budget.nRetry, turns_rolled_back: budget.nTurnsRolledBack },
    };
    const file = join(ctxDir, VIEW_NAME);
    writeFileSync(`${file}.next`, JSON.stringify(view), { encoding: "utf8", mode: 0o600 });
    renameSync(`${file}.next`, file);
  }

  /** budget.rollback_to_margin + _note_rollback: drop the newest raw turns until the margin is
   * free, never leaving a tool call without its result, and pin the ledger. */
  function rollback(raw, template, system) {
    const target = Math.max(budget.strictTarget - budget.rollbackMargin(), 0);
    let to = raw.length;
    let from = to;
    const size = (end) => {
      const trial = new LiveView();
      Object.assign(trial, { base: view.base, inserts: view.inserts, ledger: view.ledger,
                             dropped: [...view.dropped, { from: end, to }] });
      return budget.count([system, toolsText, ...messageTexts(trial.build(raw, template)[0])]);
    };
    const floor = Math.max(PROTECT, view.base?.covers ?? PROTECT);
    while (from > floor && size(from) > target) {
      from -= 1;
      while (from > floor && raw[from - 1]?.role === "assistant" &&
             raw[from - 1].content?.some?.((part) => part.type === "toolCall")) {
        from -= 1;
      }
    }
    const dropped = raw.slice(from, to);
    budget.nTurnsRolledBack += dropped.length;
    for (const message of dropped) {
      for (const part of Array.isArray(message.content) ? message.content : []) {
        if (part.type !== "toolCall") continue;
        const command = String(part.arguments?.command ?? "").split(/\s+/).join(" ").trim().slice(0, 70);
        if (command && !rolledBackCmds.includes(command)) rolledBackCmds.push(command);
      }
    }
    rolledBackCmds = rolledBackCmds.slice(-5);
    const after = size(from);
    persist({ kind: "rollback", from, to, ledger: budget.rollbackMessage(dropped.length, after, rolledBackCmds) });
    return view.build(raw, template);
  }

  pi.on("before_provider_request", (event) => {
    toolsText = JSON.stringify(event.payload?.tools ?? []);
    return undefined;
  });

  pi.on("turn_start", () => turnCalls.clear());

  pi.on("tool_call", async (event) => {
    // The reference executes only the first tool call of a turn.
    if (turnCalls.size > 0) {
      return { block: true, reason: "Only the first tool call of a turn runs; issue exactly one bash command per turn." };
    }
    turnCalls.add(event.toolCallId);
    if (event.toolName !== "bash") return undefined;
    const command = String(event.input.command ?? "");
    const refusal = rule.refusal(command, ctxDir);
    if (refusal) return { block: true, reason: refusal };
    const editable = sent.messages.slice(PROTECT + (view.ledger ? 1 : 0));
    const rendered = renderEditable([null, ...editable], 1);
    writeFileSync(ctxFile, rendered, { encoding: "utf8", mode: 0o600 });
    mirror = { rendered, covers: sent.raw.length, command };
    // The command runs exactly as the model declared it: the owner authorizes each call
    // against its declaration and refuses one that differs ("Native tool request/event differs
    // from the declared call", CLM-ref2 2026-11-20), so the reference's persistent-shell
    // wrapper does not apply; its system prompt promises no persistence either.
    return undefined;
  });

  pi.on("tool_result", async (event, ctx) => {
    if (event.toolName !== "bash" || !mirror) return undefined;
    const { rendered, covers, command } = mirror;
    mirror = null;
    const text = (event.content ?? []).filter((part) => part.type === "text").map((part) => part.text).join("");
    const { block, code } = formatToolResult(text, event.isError);
    const system = ctx.getSystemPrompt?.() ?? "";
    const before = budget.count([system, toolsText, ...messageTexts(sent.messages)]);
    let readBack = null;
    try { readBack = readFileSync(ctxFile, "utf8"); } catch { readBack = null; }
    let note = "";
    let taskNote = "";
    const touched = command.includes("LIVE_CTX_MAIN") || command.includes(CTX_ROOT);
    if (readBack !== null && readBack.trim() !== rendered.trim()) {
      const kept = CurrentTask.among(sent.raw, covers).keptIn(readBack);
      readBack = kept.text;
      if (kept.restored) {
        taskNote = "\n[LIVE_CTX_MAIN.txt: your edit removed the request you are working on; it was restored as the last user turn. Compact everything else, never the current request.]";
      }
      const turns = (t) => normalizedTurns(t).map((turn) => turn.text);
      const baseline = budget.count([system, toolsText, ...messageTexts(sent.messages.slice(0, PROTECT)), ...turns(rendered)]);
      const after = budget.count([system, toolsText, ...messageTexts(sent.messages.slice(0, PROTECT)), ...turns(readBack)]);
      const limit = budget.strictTarget;
      const unkept = acceptanceProblems(rendered, readBack);
      if (unkept.length) {
        note = acceptanceNote(unkept, before);
      } else if (after > baseline && !(limit && after <= limit)) {
        note = rejectedNote(baseline, after, before, `An edit must FIT the ${limit}-token limit`);
      } else {
        persist({ kind: "edit", covers, text: readBack });
        budget.noteCompaction();
        rule.compacted(baseline - after);
        const count = normalizedTurns(readBack).length + PROTECT;
        if (after > baseline) {
          note = `\n[LIVE_CTX_MAIN.txt: edit applied but it GREW context ~${baseline}->${after} tokens (it fits, so it was kept). If you meant to condense, you likely duplicated content instead of replacing it.]`;
        } else if (limit && after > limit) {
          note = `\n[LIVE_CTX_MAIN.txt: edit applied — context ~${before}->${after} tokens, but STILL OVER the ~${limit}-token limit. Compact more NOW (delete stale turns/outputs) or you'll get one final turn and the session ends.]`;
        } else {
          note = `\n[LIVE_CTX_MAIN.txt: edit applied — context ~${before}->${after} tokens, ${count} turns]`;
        }
      }
    } else if (touched) {
      note = code !== 0
        ? `\n[LIVE_CTX_MAIN.txt: NO change — your command exited ${code} (see error above), so nothing was compacted (still ~${before} tokens).]`
        : `\n[LIVE_CTX_MAIN.txt: NO change — your edit matched nothing, so context is still ~${before} tokens. Match text you have already seen, or target the real turn headers, which look like \`[[CTX_TURN 12 role=assistant]]\` (turn index first, then role).]`;
    }
    const template = [...sent.raw].reverse().find((message) => message.role === "assistant");
    const shown = view.build(sent.raw, template)[0];
    const pending = pendingAssistant(ctx);
    const held = budget.count([system, toolsText, ...messageTexts(shown), ...(pending ? [renderedText(pending)] : [])]);
    const observation = Observation.entering(
      { block, code }, text, budget.strictTarget - held, budget.strictTarget, (part) => budget.count([part]), outputs);
    // Output the command itself spelled out (echo "...", cat <<EOF) is the model's own words,
    // already in its context as that command: nothing new was brought in.
    const spoken = text.trim().length > 0 && command.includes(text.trim());
    if (!touched && !spoken) rule.observed(observation.tokens);
    const tokensNow = held + budget.count([observation.text]);
    const over = tokensNow <= budget.strictTarget ? "" : ` — OVER; compact ${ctxFile} now`;
    const readout = `\n[context: ~${tokensNow}/${budget.strictTarget} tokens${over}]`;
    return {
      content: [{ type: "text", text: observation.text + readout + note + taskNote + rule.reminder(ctxDir) }],
      isError: false,
    };
  });
}
