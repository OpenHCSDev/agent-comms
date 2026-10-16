Maintenance results; prove each in the code, not as preferences.

- One fact has one owner; consumers read it from there.
- Merge owners only when all required answers agree; share implementation but keep identities when only code agrees, otherwise separate.
- Count the edits a new case needs; that count is the design.
- A registry, forwarder or generator does not lower the count of outside answers.
- Crossing capabilities need multiple parents (mixins, C3 MRO).
- Shared behaviour is factored out with inheritance; split mixed-meaning classes; choose behaviour by polymorphism, never string switches, rosters or lookup tables.
- No fallbacks, mirrors, second registries, caches or forwarding wrappers.
