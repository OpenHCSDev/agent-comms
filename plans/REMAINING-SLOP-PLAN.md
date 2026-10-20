# agent-comms: remaining slop, its owners, and how to remove it

Audited head: `39082ce7c` (main, 2026-10-20 15:34 UTC). Measured with the refactor-audit census and overlay under Python 3.14, every finding below checked in the code.

## Where the codebase stands

Production code grew 45% in a week (76,580 to 111,304 lines under `src/`) and still got cleaner. Since `9fd3e0f29` (Oct 13): `type(x) is` checks −85, `None` identity checks −382, string-key subscripts −85, literal `.get("key")` −68, long boolean chains −31, absence probes −84, while classes rose by 1,813 and abstract bases by 112. Two measures went the wrong way: flattened families +20 and string dispatch +2.

What remains is concentrated. Every item below is a fact with two owners; the section says which owner keeps it and what gets deleted.

## How to execute without churn

- **No stored-data changes unless named.** Nothing in waves 0 to 2 changes a stored format, so nothing needs a carry step, a migration or extra install rehearsal. Each surface is one PR through the normal batch.
- **Mechanical rewrites go through one codemod, not by hand.** Wave 1's field directives are about 500 sites of the same shape; a script rewrites them in one commit, and the readers change in the same PR.
- **Delete first.** Wave 0 is deletions only and goes in one PR.
- **One owner per surface, disjoint files, run in parallel.** Where two surfaces touch one file, the table under each wave says which goes first.
- **Guards are the existing ratchet.** Each surface adds its measure to `tools/debt_ratchet.py` and drives it to zero. No new CI.
- **External formats are honoured exactly.** Claude Code transcripts, MCP content blocks, OAuth discovery paths, JSON Schema keywords and SQLite are other people's contracts: decode them once at entry into a typed record and never pass the dict further. Formats where we own both ends (the Toad wire, bus records) change freely, in lockstep, in one PR.

## Wave 0: delete (one PR, under an hour)

**0.1 The rules archive.** `src/agent_comms/commander/rules-archive/` holds verbatim copies of the 63 rule files that `rules/` replaced, "kept for history". Git already keeps history, so every rule now exists twice in the tree, and agents read the tree. Owner: `commander/rules/`. Delete the directory.

**0.2 The repository as a notebook.** Fleet state and run output are committed as documents: `docs/agents/fleet-board.md`, `docs/agents/fleet-narrative.md`, `docs/agents/handoff-20261018-1830.md`, `FOUNDATION-S6-HANDOFF.md`, `P520-HANDOFF.md`, `PR-BODY.md` at the root, and 78 log, stdout, stderr and `results.json` files under `docs/refactor/` (180 files in that tree). The board and its history are owned by Core; a PR description is owned by its PR; run output is owned by the run. Delete them, and add a ratchet that refuses board, narrative, handoff and log files anywhere in the repository.

**0.3 Dead modules.** `acp_ingress.py` (164 lines) and `cli_bridges/opencode_store.py` (103) have no importer, launcher or entry point. Delete. (The overlay also flagged `commander/judge.py`, `commander/rules.py`, `commander/tool_watch.py`, the parity rows and two context segments; those are live, loaded by name through `commander/cli.py`'s package import and by declared-family discovery.)

**0.4 Transition scaffolding.** `commander_parity/` (+1,414 lines this week), `tools/carry_rts_orders.py` and `tools/cutover/rts-launchers.patch` exist only for the switch. Each gets a docstring naming its deletion condition now ("delete when both machines run the native commander"), and is deleted that day.

## Wave 1: shared owners (three PRs in parallel)

**1.1 Field directives are strings.** About 500 dataclass fields carry codec and storage instructions as string keys: `metadata={"sql": …}` 218 times, `"wire_name"` 138, `"wire_omit_default"` 81, `"wire_required"` 12, `"registration_inheritance"` 12, `"snapshot_exclude"` 9, `"content_exclude"` 8, `"thread_slice"` 6, and `field_codec.add_argument` reads `flags`, `group`, `parser_default`, `parser_default_factory`, `parser_options` the same way. The values are mostly typed already (`"sql"` maps to a `Column`); the vocabulary is the restated fact, spelled at every field and again in each reader (`typed_table.py:359-364`, `field_codec.from_declaration`, `field_codec.add_argument`).

Owner: each directive is its own class, and the class is the key. `FieldCodec` declares `WireName`, `WireOmitDefault`, `WireRequired`, `SnapshotExclude`, `ContentExclude`; `TypedTable` declares `Column`; the CLI declares its parser directive. Fields attach them with `Annotated[T, WireName("x"), Column(...)]`, and readers find a directive by its type. A codemod rewrites the 500 sites in one commit; the readers change in the same PR. Wire names and column names are unchanged, so no stored data moves. Guard: zero string keys in field metadata.

**1.2 Decode once at every boundary.** Raw dicts walked by string key after they enter:
- `channel_coding_tools.decode_request` checks `set(value) == {"token", "call_id", "name", "arguments"}` by hand, a second owner of the shape `cli_bridges/codex_rollout.py::FunctionCallItem` already declares. Decode into that record through `FieldCodec`; delete the key-set check.
- `ui_model/delivery.from_wire` reads `currentScope`, `inputs`, `historicalInputs` and two counts by key. We own both ends: declare the record, change Toad and Core together.
- `cli_bridges/claude_protocol.local_command`, `image_inputs.prompt_images` and `native_package.declared`: external or packaged formats, decoded once into records at entry.
- `bus_page_index.record` reads `from`, `seq`, `to` by key: declare the page record.

Guard: the overlay's raw-shape count at zero outside the codec itself.

**1.3 Two parsers of one command line.** `commander/send_guard.py` dispatches on `{"python": …, "bash": …, "sh": …}`, checks `program.startswith("python")` and reads other processes' environments from `/proc`; `storage_rules.effects` and `storage_rules.decode` separately switch on interpreter flags (`-B -E -I -s -u`, `--reason`, `-b`). Owner: `ShellInvocation`, which already splits commands, gains a declared program family (a Python program with its flag family, a shell program) that both callers use. Delete both string tables.

## Wave 2: core state (parallel, by file)

**2.1 Absence as `None` where absence has behaviour.** 246 absence probes across 112 files, heaviest in `typed_table.py` (9), `backend.py` (9), `input_drain.py` (7), `turn_watchdog.py` (6), `commander/order.py` (6), `attempt_start.py` (6), `todos.py` (5). Where a missing value changes what the code does, the absent case becomes a declared member that carries that behaviour, the pattern the tree already uses in `NoBackup`, `NoOrderDeletion` and `UnplannedPhase`. A plain optional value with no behaviour stays `None`. Do the seven files above first; the remaining 1,094 `None` checks are measured per file and only touched where they decide behaviour.

**2.2 Families flattened at their boundary.** 35 sites, 12 in `typed_table.py` (`member.value` read out of declared enums, string-valued members checked by `isinstance`), then `channels.py` (4), `coordination_schema.py` (3), `tools.py`, `pi_events.py`, `field_codec.py`. The family owns the fact; the boundary turns it into strings and every consumer decides it again. Carry the class across and let `FieldCodec` encode it once.

**2.3 Record shapes inside core code.** `todos.py:999` writes the staged install record as `json.dumps({"at": …, "installer": …})`; `connector.py` (6 key subscripts), `tools.py` (5), `request_progress.py` and `view_unread.py` hold similar shapes. Each becomes a typed record encoded by `FieldCodec`. The staged install file is stored data: it is written and read by the same install, so it is reset, never carried.

**2.4 Switches over types.** `commander/order.py::overrides` and `::own` switch on Python AST node types: use `ast.NodeVisitor` methods, which are the AST's own polymorphism. `shared_ledger.remove_threads` switches on its value's type: give the value family the method.

**2.5 One god function.** `commander/order.py::send` is 146 lines. Its parts belong to the typed order record P290 introduced; move each to the order type that owns it.

Crossing: 1.1 and 2.2 both touch `typed_table.py` and `field_codec.py`, so 1.1 lands first. 2.1, 2.4 and 2.5 all touch `commander/order.py`: one owner takes all three.

## Wave 3: external boundaries, lower priority

- `locked_store._canonical_schema` switches on 12 JSON Schema keywords. The keywords are an external standard; declare them once as a keyword family that owns each keyword's canonical form.
- `pi_events.py`, `pi_payloads.py`, `pi_commands.py` and `native_runtime_input.py` hold most of the remaining string-keyed literals (`pi_events.py` 36, `native_runtime_input.py` 23, `pi_commands.py` 19). Decide whether the Pi runtime's wire is ours or external; either way, decode once at entry and nowhere after.
- Broad `except Exception`: 65 sites, 14 in `child_process.py`. Each either names the error it expects or is deleted.

## Done means

Each surface's guard is at zero on `main`, its deleted code is gone with no alias left behind, and the census at the new head shows the measure down by the surface's count. Report lines deleted first.

## Order of work

| Wave | Surfaces | Parallel | Stored data touched |
|---|---|---|---|
| 0 | 0.1 to 0.4 | one PR | none |
| 1 | 1.1, 1.2, 1.3 | three units | none |
| 2 | 2.1 to 2.5 | by file; one unit owns `commander/order.py` | only the staged install file, reset |
| 3 | external boundaries | as capacity allows | none |
