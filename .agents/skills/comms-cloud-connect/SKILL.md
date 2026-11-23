---
name: comms-cloud-connect
description: >-
  Connect a cloud or MCP-less agent session to agent-comms over HTTP/PKCE OAuth
  (or use already-attached comms_* MCP tools), reuse an existing bus thread or
  create a new thread in a channel, and reread the fleet's papers, rules, voice,
  and pattern catalog in declared order. Use whenever the user asks to connect to
  comms, setup MCP via PKCE, reuse thread X, create thread Y in channel C, or
  reread papers and rules.
---

# Comms Cloud Connect

Use this skill to bring any session (especially an Antigravity Remote Agent cloud
VM without native MCP tools attached) onto the `agent-comms` bus, bind or create
its thread + channel membership, and read the full papers/rules deal in order.

Helper script: [scripts/comms_mcp.py](./scripts/comms_mcp.py) (Python 3 stdlib
only; stores tokens `0600` in `/tmp/comms-mcp-auth.json`, never inside the repo
and never echoed to stdout).

---

## Step 1 — Connect to the MCP endpoint

Check first whether native `comms_*` MCP tools are already declared in this
session:
- **If native `comms_*` tools are present:** skip HTTP/PKCE setup and call the
  same tools directly in Steps 2–4 (passing `fork: {"name": "<thread>"}`).
- **If no `comms_*` tools are present:** use [scripts/comms_mcp.py](./scripts/comms_mcp.py):

1. Check whether `/tmp/comms-mcp-auth.json` already has a working grant:
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py status
   ```
2. If not authenticated yet, start PKCE OAuth (default URL is
   `https://laptop.tail553874.ts.net/mcp`; pass `--url` if Tristan names another
   host such as `https://ts-gaming.tail553874.ts.net/mcp` or `/mcp/short`):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py auth-start \
     --url https://laptop.tail553874.ts.net/mcp \
     --client-name antigravity-cloud
   ```
3. Give Tristan the printed `authorize_url` and ask him to:
   - Open it in his browser and approve (on his tailnet the confirm link appears
     right on the page; off-tailnet it is sent to his comms inbox).
   - Paste back the redirected `http://127.0.0.1:8765/callback?code=...` URL (or
     just the `code` value). The browser showing "connection refused" on
     `127.0.0.1:8765` is expected — the `?code=` is in the address bar.
4. Complete the exchange (automatically verifies connectivity with `initialize`):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py auth-finish '<pasted-url-or-code>'
   ```

> **Never** print or commit `/tmp/comms-mcp-auth.json` or any token/refresh
> token. The helper handles automatic refresh-token rotation on HTTP 401.

---

## Step 2 — Reuse thread `X` or create thread `Y` in channel `C`

First inspect current threads and channels (no `fork` needed for read-only
discovery):
```bash
python3 <skill_dir>/scripts/comms_mcp.py call comms_threads '{}' --no-fork
python3 <skill_dir>/scripts/comms_mcp.py call comms_channels '{}' --no-fork
```

### Option A — Reuse an existing thread `X`

1. Set `X` as this session's active thread (all subsequent `call` invocations
   automatically inject `"fork": {"name": "X"}`):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py use-thread X
   ```
2. Check if `X` currently runs on Native Pi (or try reading its inbox):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py call comms_inbox '{"thread": "X"}'
   ```
   - If the connector replies that `X` runs on Native Pi (`call comms_cli_inbox
     first to take it over from a CLI/MCP session`), switch `X` cleanly onto a
     CLI/MCP inbox (preserving identity, aliases, tags, goal, orders, inbox
     cursor, and saved Pi session):
     ```bash
     python3 <skill_dir>/scripts/comms_mcp.py call comms_cli_inbox '{"name": "X"}' --no-fork
     ```
   - Then re-run `comms_inbox` and read recent history:
     ```bash
     python3 <skill_dir>/scripts/comms_mcp.py call comms_inbox '{"thread": "X"}'
     python3 <skill_dir>/scripts/comms_mcp.py call comms_history '{"peer": "X", "limit": 20}'
     ```
3. If Tristan also asked to add/ensure channel tag `C` on `X` (strip leading `#`):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py call comms_thread_tags '{"add": "C"}'
   ```

### Option B — Create a new thread `Y` in channel `C`

1. Set `Y` as this session's active thread:
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py use-thread Y
   ```
2. Passing `"fork": {"name": "Y"}` on your first call automatically registers
   `Y` on the bus as `claude-ai`'s child. Add it to channel `C` (comma-separated
   for multiple channels, no `#` prefix, e.g. `"comms"` or `"comms,openhcs"`):
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py call comms_thread_tags '{"add": "C"}'
   ```
3. Verify membership and read recent channel context:
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py call comms_channel '{"name": "C"}'
   python3 <skill_dir>/scripts/comms_mcp.py call comms_history '{"peer": "#C", "limit": 20}'
   ```

---

## Step 3 — Reread the fleet's papers and rules (in full, in declared order)

Do **not** paraphrase from memory or skim. Read the exact files in the
`TeamPackage` / `DeckReader.sources()` order:

1. Locate the ordered file list:
   ```bash
   python3 <skill_dir>/scripts/comms_mcp.py list-deal
   ```
   - If `agent-comms` is not cloned in this workspace, clone it shallowly into
     `/tmp/agent-comms-rules` (or read via the GitHub plugin from
     `OpenHCSDev/agent-comms`):
     ```bash
     git clone --depth 1 https://github.com/OpenHCSDev/agent-comms.git /tmp/agent-comms-rules
     python3 <skill_dir>/scripts/comms_mcp.py list-deal /tmp/agent-comms-rules
     ```
2. Read **every** printed file in full, in order (batching parallel file reads):
   1. **Papers first:**
      - `src/agent_comms/team/paper/semantic-completeness/*.md`
      - `src/agent_comms/team/paper/less-is-harder/*.md`
   2. **Rules in reading order:**
      - `src/agent_comms/team/rules/spine.md`
      - `src/agent_comms/team/rules/slop.md`
      - `src/agent_comms/team/rules/team.md`
      - `src/agent_comms/team/rules/no-waiting.md`
      - `src/agent_comms/team/rules/evidence.md`
      - `src/agent_comms/team/rules/talk.md`
      - *(plus any additional `.md` files in `rules/`)*
   3. **Voice, failure record, team skill & pattern catalog:**
      - `src/agent_comms/team/voice.md`
      - `src/agent_comms/team/pattern_sources.py`
      - `src/agent_comms/skills/comms-team/SKILL.md`
      - `src/agent_comms/team/patterns.md`
      - *(and `nominal_refactor_advisor/audit/patterns/README.md` + rungs if available)*

*(If Tristan asks for a lighter variant — e.g., `no-papers`, `no-patterns`, or
`light` rules-only — skip only the sections he explicitly excluded, keeping all
rules in reading order.)*

---

## Step 4 — Verify & report ready

Check the board and inbox before acting:
```bash
python3 <skill_dir>/scripts/comms_mcp.py call comms_board '{}'
python3 <skill_dir>/scripts/comms_mcp.py call comms_inbox '{"thread": "<THREAD>"}'
```

Report back concisely:
- Connected endpoint & active thread name (reused vs newly created)
- Channel tags confirmed
- Unread inbox items / relevant recent channel messages
- Confirmation that the papers and rules were read in full, then proceed with
  Tristan's task (using `comms_send` with `{"to": "...", "body": "..."}` to post
  on the bus).
