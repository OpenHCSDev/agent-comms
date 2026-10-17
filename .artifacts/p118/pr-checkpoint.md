## P118 ownership checkpoint — budget still FAILS

Head `902e5ad9ce88916e1fca70d0cd1c0c0afb99194a`. Not ready for merge.

Native custody is net **−5**, DurableHost count **0**, all three introduced Any annotations removed. Native TurnSession again owns its original receiver and response correlation. Existing PiRpcChannel owns Durable streaming/correlation; queue shutdown eliminates per-event waiter tasks. No new module, wrapper host, stored format or provider request. Native key/attestation stay on PiSessionChild.

NRA AST before/after: **332 modules, zero omissions**. SDK JavaScript producer read semantically, outside this Python parser; no JS edited. `_read_task` now has one declaration, in PiRpcChannel; no native child queue or PacketRead/WakeRead transport remains. Common custody inspection is owned once on NativeCustody, with state-specific borrowed/retiring overrides.

Final bounded batch **75 passed / 12.08s**, including actual SDK host context/error, streaming watch/configure/reopen, held-writer history, CLI subprocess imports, and actual native tool-effect terminal failure. An initial native correlation regression (74 pass / 1 fail) was repaired and included in the final complete batch. Not installed-default or live-provider acceptance.

**Ratchet exit 1: SourceLines net +537, 137 above the unchanged 400 allowance. No other introduced measure.** Model/configuration/history reduction is unfinished; transcript_receipts +34 and native_entries +22 remain. InputId/send-disposition worker integration remains explicitly unfinished.

Per-file assessment against inherited ratchet base `0ec36f573`:
```
1	1	src/agent_comms/__init__.py
7	1	src/agent_comms/agent_backend.py
1	1	src/agent_comms/bus_publication.py
1	1	src/agent_comms/channel_coding_tools.py
1	12	src/agent_comms/external_transcript.py
11	1	src/agent_comms/jsonl_stream.py
45	50	src/agent_comms/native_custody.py
22	0	src/agent_comms/native_entries.py
1	1	src/agent_comms/native_pi.py
4	0	src/agent_comms/native_session_files.py
21	31	src/agent_comms/native_transcript.py
1182	0	src/agent_comms/package-lock.json
10	0	src/agent_comms/package.json
13	14	src/agent_comms/pi_commands.py
366	0	src/agent_comms/pi_durable_backend.py
155	0	src/agent_comms/pi_durable_host.mjs
103	43	src/agent_comms/pi_events.py
13	12	src/agent_comms/pi_payloads.py
66	16	src/agent_comms/pi_rpc.py
3	2	src/agent_comms/schema_report.py
1	1	src/agent_comms/selected_tool_broker.py
1	1	src/agent_comms/selected_triage.py
1	1	src/agent_comms/selected_write_plan.py
5	0	src/agent_comms/transcript_events.py
34	0	src/agent_comms/transcript_receipts.py
3	2	src/agent_comms/turn_runner.py
4	0	src/agent_comms/unstarted_runtime.py
```

Local source/AST/results/custody notes: `/home/ts/wt/core-p118-owner/.artifacts/p118/checkpoint.md`. Other owners’ checkouts and installed defaults untouched.
