# Provider service tier

A thread owns its requested service tier with its model and thinking selection. New and legacy rows default to `priority`; `standard` is an explicit supported value. The Pi extension adds `service_tier: "priority"` only for Pi models whose declared API selects an OpenAI Responses request serializer. It does not infer support from provider names. Standard requests omit the field and retain provider defaults.

## Output limits remain Pi-owned

Agent-comms does not rewrite provider output limits. Pi's model catalog and request options own them.

For `openrouter/z-ai/glm-5.3-flash`, Pi's effective model store currently declares `maxTokens: 943718`. When the prompt leaves less room, Pi's `buildBaseOptions()` computes a smaller request value from the context window, estimated input, and its safety reserve. That runtime clamp—not an independent `774600` catalog value—is the source of observed requests near 774,600 tokens. OpenRouter reserves credit against the requested maximum, so an operator who needs a lower default must override this model's `maxTokens` in Pi's `models.json` until Pi distinguishes the model's technical maximum from its default request reservation. Core must not retain or project a second output-limit field.

## Deployment compatibility

Adding `service_tier` changes the strict `Thread` registry wire schema. New processes can read rows written before this field existed because the dataclass default supplies `priority`. Older processes cannot read rows written by the new version: `FieldCodec` rejects unknown fields, including `service_tier`.

Deploy this change with a full agent-comms process restart before any new process writes `registry.json`. Do not run old and new Core processes against the same registry during the upgrade. This is a process-version requirement only; existing registry rows and saved Pi sessions do not need migration.
