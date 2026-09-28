A missing or drifted runtime schema used to fail inside the background drain, reach only an optional debug log, and leave the owner displayed as Ready.

Record a typed, owner-scoped drain diagnostic in the existing activity log. Repeated failures are deduplicated, survive unrelated activity and stale-event expiry, and clear after successful observation by the same owner. Expected boundary failures remain observable while the watcher waits for recovery; unexpected exceptions are recorded then re-raised. No schema repair, input replay, or parallel status store is introduced.

The existing ThreadPresentation carries attention independently from busy. A paired Toad change preserves that diagnostic over ACP Ready updates and highlights the existing session details.

Validation: **17 installed-wheel tests passed** (21.96s), including actual missing/drifted SQLite schema -> background InputDrain -> fresh ThreadView -> explicit schema restore -> Ready. Paired [Toad PR128](https://github.com/OpenHCSDev/toad/pull/128) has **2 installed mounted pilots passing** on current T4 main (45.95s). Both conversation and DM warning/recovery and ACP Ready preservation are covered. No remaining diagnosed blocker. Current main afe0688a is incorporated; no source conflicts. CI deferred. native_pi.py is untouched by this branch.
