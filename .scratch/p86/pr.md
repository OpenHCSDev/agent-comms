## P86 cli/runtime group — partial checkpoint

This is NOT P86 acceptance. The 23:00 deadline was missed. `cli_commands.py` and `runtime.py` still contain Any; this draft retains the working request-family change for continuation.

Runtime request payloads/results are actual JSON, now declared as Pydantic JsonValue. The existing SocketClient owns sending; delete RuntimeRequestContext.send and migrate every production caller to ctx.client.send. Decode still belongs to the existing FieldCodec. No new codec, aliases, casts, ignore directives, object substitutions or unrelated unions.

Comparison: base 4d691d469c9af160246e546614815506ef3dc038 to 9f8d076f2.
- src: +23/-25 = -2, one file.
- tools: 0; tests: 0.
- AnyAnnotation runtime_requests.py: 19 -> 0. cli_commands.py/runtime.py unchanged.
- Existing NRA census before edit: 1,064 Core/Toad/dependency modules, no parse omissions; 2,532 declarations, 3,016 references. Syntax evidence does not resolve arbitrary dynamic aliases. Retained locally in .scratch/p86/before.json.
- Consumer search: rg 'ctx\\.send|RuntimeRequestContext|proxy_payload|ResultRuntimeRequest' src tests /home/ts/wt/toad-live/src. RuntimeRequestContext.send removed; no ctx.send remains in production runtime requests. Source owner runtime.py SocketClient.send receives the existing payloads directly.

Checks: 20 request roundtrip, malformed decode, and actual Unix-socket/proxy dispatch cases passed (tests/test_command_families.py -k 'runtime_wire_format or invalid_runtime_parameters or one_runtime_declaration'). Ruff F401/F821 and whitespace passed. Exact-head ratchet passed, AnyAnnotation -19 and source -2.

Unmet: installed CLI/runtime journey; complete cli/runtime group; further source census after complete migration. Wider combined test command was stopped at 60 seconds after showing unrelated failures/errors; no passing full suite is claimed. Runtime wheel had no pytest, so the focused source check reused the existing development interpreter. No installation, pointer change or uncertain input replay.
