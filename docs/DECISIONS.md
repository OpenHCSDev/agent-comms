# Owner decisions

## 2026-09-29 — Ship useful performance progress

Owner: "regrding eprformance, we can merge prs once substantia lprogress is made even thoug its not the final taarget so at least the live intal lcan make sue of it and anothe pr contining hte work can be opened"

Merge coherent, substantial performance improvements after focused local and
actual affected-path verification, install them, and continue remaining work in
a follow-up PR. The final performance target is not a gate for useful progress.
Report measured results and remaining scope honestly; this does not complete the
overall performance or nominal refactor goal. CI remains deferred.

## 2026-09-28 — D22: round-two durable wire history

Owner: "Rewrite once into the current format, preserving history in place (plan default)"

Implement one tool in `tools/cutover/`, run at the completed step's quiet install,
verify history preservation, then delete the tool. No pre-cutover reader remains
in production source. No runtime state is treated as permission to replay inputs.

## 2026-09-28 — Keep the refactor aggressive throughout implementation

Owner: "its notmral its not fuly yet there, it wil take a bit of time, i jsut wnt to make sure we don't fall into bad habits and we keep stying very aggressive"

Incomplete surfaces are expected while work continues; they do not relax the
architecture or deletion requirements. Review new code for behavior owned by
polymorphic declarations, inherited shared implementation, declaration-derived
membership, and complete removal of replaced mechanisms and callers. Correct
new duplication during the assigned batch. Keep implementation moving without
turning this review into repeated testing or coordination ceremony.

## 2026-09-29: coherent delivery and continuous real user journeys

Tristan: "i need more strategy and less local optimization, eveyrone is optmizgin for local busywork too much ,find the pi system prompt and read it. please no more fucking arouind"

Tristan: "way too muhc fuckign busywork for the amount tokens going in eveyrone stop being so cauatious and start optimizing harder in your deicoins, ceremony must hve value otherise its perofmrative and i don't cre"

Tristan: "tests passing isn't correct. tests passing is to test yo ursanity and help you tes real user poerly. live testing is not optiojal. we must alrady ahve infra for this . it can do basic ass tests using saved state  and mock responses and what not its not reocekt science  we need continuous coerage in tests not many small pieces thats terrible"

Read project Pi prompt `.pi/APPEND_SYSTEM.md`. Tesla156 owns the coherent
workspace/navigation/retained-presentation integration; Carver153 implements
first-open/tab identity and coordinates the overlapping navigation callers.
Extend existing normal App/Pilot and ACP/native fixture infrastructure into one
continuous representative saved-state user journey. Controlled provider responses
are allowed; UI, saved state, protocol and installed entry paths must be real.
Exercise startup history, channel/sidebar clicks, participant/thread opening,
A/B/A return with reader/draft retention, fork/first input and reply/status/history
without skipping intermediate user actions. Actual live opening check before
activation is required. Focused tests support it. Ship usable coherent tranches
without waiting for final latency, full refactor scope or deferred CI.
