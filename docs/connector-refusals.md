# Connector refusals and changed-source maintenance

PR: https://github.com/OpenHCSDev/agent-comms/pull/1922
Owner: pi-connector. Branch: fix/connector-refusals. Not installed or merged here.

## Causes and repair

- Base `connector.py:66-71` raised bare ValueError for a refused directive.
  `ToolsCallMessage.result` caught only UnregisteredThreadError, so aiohttp
  returned HTTP 500. `errors.py:6` now declares ToolRefusedError; intentional
  directive, review, tool-input and named lookup refusals derive from it.
  `connector.py:301` catches only that type. Other failures remain real errors.
- `todos.py:933` owns the directive raising check; its order and connector
  consumers use it. `RuleReviews` remains the owner of age/limit/next-step text.
  The send refusal path reads only that review, not all standing rules. No
  standing-rule subprocess or history derivation was added before answering.
- `CandidateCheckpoint.require_source` compares root/device/inode. An identical
  bus prefix moved to a new inode, as install/carry can do, invalidates the
  disposable index. FirstBuild previously resumed that stale checkpoint and
  killed the worker. `wake_candidate_index.py:211` now selects RebuildIndex for
  a changed verified source, inside the existing captured-source and revision-
  checked batch. Strict resume and readers still refuse stale sources; damaged
  schemas are not silently repaired. The worker logs named projection failures.
- While the worker was dead, its optional candidate supplement and route-count
  catch-up stopped. Canonical original delivery did not depend on that index;
  another append could start another worker, which would hit the same stale row.

## Original refusal records

`coordination_tables/refusals.py:26` declares SendRefusal in the existing
coordination.sqlite3 delivery-outcome database. The decoded send path in
`tools.py:1085` writes exactly one row before returning its refusal. Fields:
`id`, `sender`, `target`, `reason`, `refused_at_ms`. Sender/target retain the
attempted boundary spellings, including a target that cannot be resolved.
No fabricated message, audience, execution or delivery identity is created.

Fresh databases discover the declaration normally. Existing roots require the
additive `tools/cutover/carry_send_refusals.py` step; it preserves every existing
row and rejects schema drift. Ordinary readers/writers do not repair schema.

Read API: `SendRefusal.recent(root, sender='', target='', limit=50)`; filters
are indexed, maximum page size 200. CLI:

```
agent-comms --root ROOT tool --tool comms_send_refusals \
  --arguments '{"sender":"claude-ai","target":"worker"}'
```

The commander page reads these originals; it does not append its own records.
Malformed tool calls rejected before a send is decoded have no send outcome.

## Real proof

`tests/verify_connector_live.py` runs the installed connector executable with
this branch selected via PYTHONPATH, its own scratch HOME/bus, loopback port,
and a scratch OAuth access grant. It uses curl, the actual protocol, real Comms,
and real production maintenance threads. No fake requests or fake Comms.

Main:

```
refused wording:
500 Internal Server Error
Server got itself in trouble
500 0.135152
expired review:
500 Internal Server Error
Server got itself in trouble
500 0.021311
source replacement: old inode=10376782, new inode=10376831; candidate bus source changed
maintenance threads started=1, failures=['ProjectionRebuildRequiredError: candidate bus source changed']
```

Branch, including refusal recording:

```
{"id":7,"result":{"content":[{"text":"{\"error\":\"Directive refused, nothing sent: Jargon ['candidate'] hides what happened; say it plainly.\",\"type\":\"failed\"}","type":"text"}],"isError":true},"interpreter":"/home/ts/code/projects/agent-comms/stack/.venv/bin/python","jsonrpc":"2.0"}
200 0.163583
agent-comms readback: {"id":1,"reason":"Directive refused, nothing sent: Jargon ['candidate'] hides what happened; say it plainly.","refused_at_ms":1792432468291,"row":"send_refusal","sender":"claude-ai","target":"worker"}

{"id":7,"result":{"content":[{"text":"{\"error\":\"Directive refused, nothing sent: RULES DUE (last rule review was 21 min ago (max 20)) -> agent-comms-commander rules review and answer the drawn rules before any order\",\"type\":\"failed\"}","type":"text"}],"isError":true},"interpreter":"/home/ts/code/projects/agent-comms/stack/.venv/bin/python","jsonrpc":"2.0"}
200 0.019229
agent-comms readback: {"id":2,"reason":"Directive refused, nothing sent: RULES DUE (last rule review was 21 min ago (max 20)) -> agent-comms-commander rules review and answer the drawn rules before any order","refused_at_ms":1792432477310,"row":"send_refusal","sender":"claude-ai","target":"worker"}

invalid tool arguments: 200 0.004700
malformed stored schema remains HTTP 500
source replacement: old inode=13641466, new inode=13641490; candidate bus source changed
maintenance threads started=1, failures=[]
same maintenance thread completed normally: last_seq=1, caught_up=True, no restart
```

The probe also checks no refused send allocated a message sequence, exact
response/record text equality, and exactly two rows for two decoded refusals.
It approves the identical replacement prefix through the canonical source
barrier, then observes one normal worker drain, not a restart loop.

## Checks and source evidence

- Before implementation, reused refactor-audit Package.load/ParsedModule with
  Python 3.14: all 379 Core production modules parsed, zero omissions. Enumerated
  every class and all declarations, writes, imports, decisions and references
  in the connector/review/candidate families; expanded for coordination storage.
  Dynamic attribute matches are syntactic, not runtime binding proofs. NRA's
  available Python 3.12 could not parse Core generic defaults; that failed scan
  is not coverage. No external dependency owns these local decisions.
- Deleted CheckpointState.first_build and NoCheckpoint.first_build; existing
  IndexStart capabilities select the one resume/rebuild implementation instead.
  One ToolRefusedError declaration; one SendRefusal declaration and original
  writer, no page store. Source budget explicitly declared for this added work.
- Initial focused batch: 62 passed, including real connector subprocess/curl,
  strict stale-source tests, schema drift, concurrent maintenance and review age.
- Final record/carry batch: 25 passed, including the real connector/curl/CLI
  readback journey, fresh-root refusal, additive install preserving old rows,
  idempotent install and read-without-write behavior. All test commands used
  `timeout 300` and Python `-X faulthandler`, sequentially with reused dependencies.
- Earlier wider standing-rule batch exposed four tests referencing a missing
  `/home/ts/wt/worker--P109` fixture and one live-memory-threshold race. These are
  not connector acceptance proofs and remain outside this repair. An obsolete
  full_history_page call in the owned maintenance test was replaced by the
  original message returned by its publisher.
- Separate ratchet call completed; after lint fixes its only positive measure
  was source-line growth. A subsequent commit declares that source budget.
  Final ratchet rerun remains to be recorded; shipping is not held for it per
  claude-code's latest instruction.

Scratch owner/purpose: pi-connector,
`/home/ts/.cache/agent-scratch/pi-connector`, source census and bounded verification.
Connector subprocesses exit and their scratch buses are removed by the probe.

Assumed:
- Installs replace bus sources while writers are quiet; an unverified or corrupt
  canonical source is not permission to fabricate or repair original history.
- Existing roots receive the additive refusal-table carry before this build runs.
- A refusal record describes an attempted send, not a delivered message. Raw
  implementation/storage exceptions are deliberately not disguised as refusals.
