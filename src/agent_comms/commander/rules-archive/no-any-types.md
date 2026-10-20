---
name: no-any-types
description: Owner rule - Any is not an allowed type annotation; declared fields and parameters must have a real type
type: feedback
---

`Any` is not allowed as a type annotation ("we shouldn't allow Any", owner, 2026-10-12).

**Why:** it came up when the global config form could not be built because `SourceSpatialDomain.fill_value: Any` has no widget. In OpenHCS a declared type is what generates the form control, the MCP description and the conversion, so `Any` is a missing answer the mechanism needs. More generally it hides the family that should exist.

**How to apply:** never write `Any` in new code; when touching code that has it, replace it with the real type (a union of the actual members, a nominal ABC, a TypeVar, or an array type). Sized on 2026-10-12: about 1,180 `Any` annotations in `openhcs/` and about 1,140 across the libraries, so removal is staged: first user-facing declarations (config dataclass fields, catalogued and custom function parameters) with a hard guard, then a ratchet so the count only falls. My interpretation: this covers `Any` hidden in `Optional[Any]`, `dict[str, Any]`, `-> Any`; `object` is acceptable only where any object truly is accepted. Related: , [[optimize-for-slop-elimination]].
