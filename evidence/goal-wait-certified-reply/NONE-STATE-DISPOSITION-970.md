# Item 5 — #462 final integrated None disposition

Owner: Mendel. Reviewed integration **970bc527f4ddd9b3bde5522dc671aea11fd27ece**
against original base **ea8aa4eaee1ce3eed9aef19cd44010b592f83b26**.
Accepted source remains **c53449cb6fdefac52152f79f23d66ba6ba6c9340**;
original review **a65ad88ecf8511c16650056d331e56ed78253e7e** remains retained.

## Exact scope and attribution

Own production change: **108 deleted / 121 added**, 13 files. Final `970`
versus original base on these files: **114 deleted / 123 added**. The only
later intersection is `owned_turn.py` (**6 deleted / 2 added**): Arendt replaces
the cached-info/optional-strategy model selection with actual prepared StateData.
The #462 reply/source/queue declarations otherwise have identical bytes.

`NONE-STATE-DISPOSITION-970.json` lists all files/counts/blob IDs and final
locations for **11 own added None text sites**, reusing the published 110-site
own-and-peer census. This is source disposition, not another installed test.

## Newly added sites and actual owner relation

| Final sites | Rationale and full consumer relation |
| --- | --- |
| `goal_management.py:334-355` (two checks) | Current/captured goal or applicable wait can be **absent internal declarations**. Absence returns false without mutation; it does not mean responded, ignored, failed or goal-complete. The existing registry owner, goal ID, owner birth, exact original certified message reference and addressed frozen sender relation must all match before existing wait-ID CAS. |
| `turn_input_source.py:38,61,71,85,86,150,168,180` (eight signature/check sites, including repeated signatures) | These nullable wait arguments/checks predate #462. They denote presence of the **original dependency declaration**, not SQL/JSON taxonomy. CapturedInputDependency requires its original wait ID when a wait exists; an already cleared dependency retains the prior contract. Ordinary admission still checks captured dependency identity before dispatch. Direct owner inputs remain wait-independent. The change joins certified original deliveries instead of today's mutable registry sender. |
| `selected_participant.py:53` | `-> None` is a unit return annotation. Its caller in `selected_turn.py:254` runs **after successful fenced full publication**; `selected_triage.py:62` runs after committed ignore. Preparation, failure and UNKNOWN paths do not consume a wait. |

The complete 13-file relation includes `bus_publication.py`, `goal_management.py`,
`goal_presentation.py`, `goal_waits.py`, `input_drain.py`,
`ordinary_admission_rules.py`, `owned_send_admission.py`, `owned_turn.py`,
`queued_input.py`, `selected_participant.py`, `selected_triage.py`,
`selected_turn.py`, `turn_input_source.py`.

Deleted ownership copies are **GoalReplyScope**, mutable sender inference,
direct-origin/message subset reconstruction, and **QueuedInputContext.wait**
with all handoff/validation consumers. No nullable replacement flag, second
wait/message store, seen cache or stored schema was added. The original wait
owner and certified source remain sole authorities.

## Shared defect correction and acceptance limits

The incomplete native-reference defect recorded in `NONE-REVIEW.md` is now
corrected by Arendt's **f997cc8586c1dd7551d85d0cbd7334fd215dabca**, included
in `970`. `native_input_record.py:84,148` rejects partial SQL context and
acquires a complete recorded reference; `native_runtime_input.py:189,296`,
`cursor_owner.py:76`, `native_source_cursor.py:215` close its consumers.
This explicitly supersedes the historical review's **open** builder finding;
the original witness is preserved unchanged.

**Disposition:** no new None-as-domain-state in our #462 changes; no additional
own source fix required. This is not blanket closure of historic nullable
native shapes. The actual installed native/queued ACP **11.19s** acceptance in
`READY.md`/`READY.json` is retained without rerun. Einstein owns the current
paired gate; no installed-970 result is inferred from this review.

No provider calls, public writes, source changes or SessionRevision work were
performed. Compaction journal reset remains the declared runtime cutover scope;
native sessions/input-proof/UNKNOWN, durable wire and goals remain protected.
