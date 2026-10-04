# F1: Target actions as a typed API

**Index:** [README.md](README.md). **Repositories:** agent-comms, then Toad, in lockstep. **Pattern:** BOUND-8.

## What is wrong

The ownership is right: each CLI command declares its own target actions (`thread_bindings`, `channel_bindings`, `confirmation`), and the description derives from the command's declaration through `FieldCodec.record_schema(cls)`. Everything after the declaration is untyped:

- Bindings are `dict[str, object]`; `confirmation(bound)` reads `bound['name']`.
- `describe` builds a JSON-schema dict by mutating string keys (`properties`, `multiline`, `editor_default`, `description` from `metadata['parser_options']`).
- Command results are untyped: `reconnect_targets(result)` reads `result['thread']` and `result['launched']`, restating `StartCliCommand`'s result shape in another method.
- **Toad calls the CLI command in-process** (`TargetActionsCliCommand(...).apply(comms)['actions']`) and reads `definition['command']`, `['label']`, `['confirmation']` and `['parameters']['properties'][...]` by key: 12 reads in `target_commands.py`, `thread_actions.py` and `widgets/comms_command_dialog.py`. The CLI's JSON output has become the in-process API, so every field name has two authorities, one per repository.

## Target

- **agent-comms:** a typed `TargetAction` record built once from the command declaration: label, the command class itself, the bound command (a partially bound instance, never a dict), the editable fields with their editor metadata, and the confirmation text. `TargetActionsCliCommand` encodes these records for its JSON output; a typed function returns them in-process. Command results are typed (a start result with `thread` and `launched`), and `reconnect_targets` reads attributes.
- **Toad:** calls the typed function and reads attributes. No `definition[...]` reads remain.

## Done when

No string-keyed read of an action or a command result remains in either repository, and the CLI's JSON output is produced by encoding the typed records.
