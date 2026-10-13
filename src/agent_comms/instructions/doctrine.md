Maintenance results; prove each in the code, not as preferences.

- One fact has one owner; consumers read it from there.
- Merge owners only when all required answers agree; share implementation but keep identities when only code agrees, otherwise separate.
- Count the edits a new case needs; that count is the design.
- A registry, forwarder or generator does not lower the count of outside answers.
- Crossing capabilities need multiple parents (mixins, C3 MRO).
- Shared behaviour is factored out with inheritance; split mixed-meaning classes; choose behaviour by polymorphism, never string switches, rosters or lookup tables.
- No fallbacks, mirrors, second registries, caches or forwarding wrappers.

Confirmed worked cases:

#753 A byte budget declared in two places, found by grepping the constant, let a batch smaller than one bus record stall catch-up forever; every delivery omitted its context.
The index now owns progress, always admitting one record; callers only size batches. https://github.com/OpenHCSDev/agent-comms/pull/753

#754 An omitted coordination context rendered as nothing on the delivery.
The omitted state follows the family's named visible-state instruction; catch-up drains until caught up, progress owned by the byte checkpoint. https://github.com/OpenHCSDev/agent-comms/pull/754

#749 Unread reads were split between the message bus and a CLI entrypoint that also serialized results.
MessageBus now owns notification-driven unread reads, reusing the ledger and watch owners; the old serializer is deleted. https://github.com/OpenHCSDev/agent-comms/pull/749
