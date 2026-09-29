# Channel admission with stopped/unstarted recipients

Actual live#nra originals157/158 include domain-mapping plus four registered
recipients absent from participants: nra-class-first-name-review,
nra-ordered-branch-call-review, nra-roster-heuristic-map, refactor-r1. Their missing
rows veto whole-cohort acceptance; the visible recipient itself was registered.

Existing fixtures manually register coordinator participants (sometimes against
an alternate coordinator.sqlite instead of production coordination.sqlite3),
bypassing the missing production step. New regression uses real Comms bootstrap,
thread registration, ordinary channel publication, frozen bus read and the inbox
cohort admission path. No manual participant setup or mocked storage. Installed
old core0931 reproduces exact IdentityConflict; installed candidate27testsPASS7.05s
with all existing cohort/private-human tests. Two sequential publications preserve
participant identity/generation, stopped status and no process starts.

Publisher registers all frozen recipients from the authoritative registry snapshot
before the durable append, using existing ParticipantStore.register. Inbox readers
still cannot invent identities from a message. A failure registering any recipient
prevents append; previously registered identities remain valid. Adds11production
lines to fix the missing publication step, no alternate formats/registry/reader.

One-use live operator restores only the four absent identities bound to those
unaccepted originals and matching current registry creation identities. It neither
resends messages nor rewrites history/starts owners. Executed operator deleted;
live recovery receipt and actual inbox behavior are recorded separately.
