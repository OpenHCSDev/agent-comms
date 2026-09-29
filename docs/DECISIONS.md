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
