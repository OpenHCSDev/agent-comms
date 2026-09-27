# A1/A2 foundation

Owner: foundation/S6 worker, branch codex/refactor-foundation-s6-20260927.
Base: b1e5bfd (current source, not the plans' f9854ab / 0887b81).

Stable modules:
- agent_comms.declared_family.DeclaredFamily: root affix=, member declared_name=,
  decode(name) -> class, names() -> tuple, members_with(capability) -> tuple.
- agent_comms.field_codec.FieldCodec: encode(value), decode(type, data).
  Dataclass init fields are authoritative; family objects use kind plus fields;
  field metadata wire_name declares an external alias. Unknown fields, wrong
  primitives, nonfinite numbers, malformed tuples and names fail at the boundary.
  Supports nested dataclasses, families, enums, unions, lists, tuples and str-key dicts.

Reuses metaclass_registry.AutoRegisterMeta (runtime dependency >=0.1.0).
The small metaclass adapter supplies per-family RegistryConfig and collision
policy; it does not maintain a second registry. Abstract classes are excluded by
AutoRegisterMeta; slots dataclass replacement retains the final class identity.
ImportAdapter's existing registry is specific to ImportFormat and remains untouched.
No NRA runtime dependency is introduced.

Validation: 17 focused foundation tests passed (Python 3.11, NRA venv; pytest
reports absent pytest-asyncio configuration support, no async tests in this set).
Black and Ruff passed on the four added Python files.

NRA source inspected at 52fe8b4666a20583f0ddf8ed3b7a9e89857e4809:
CLI help, getting_started, architecture playbook, PatchTargetOperation and shared
registry implementation. Paper inspected: paper1_typing_discipline/latex_jsait/
content/03_model_oopsla.tex: stable identity must survive world extension;
structural observations cannot distinguish clone cases. Here class identity and
collision-checked durable names carry the distinction; consumer shape tests do not.
The initial complete package scan exceeded a 165-second external bound (exit 124,
no JSON output); no global architectural or semantic-equivalence proof claimed.
