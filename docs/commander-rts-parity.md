# P617 RTS / installed Core command parity

Inspected 2026-10-19, installed runtime `toad-864cd9d37-core-67d8e63a7-native-951594719d8736a1-20261019T064518Z` and copied RTS snapshot `rts-source-0500`. Source checkout examined at `fe2f9fbc4e1baa226e0305ef8544fb8e2f84be12`; it is not used to launch the replacement scripts. Six old filenames now execute `/home/ts/.local/share/agent-comms/current/bin/agent-comms-commander` only.

**NOT READY TO SWAP.** An exec-only launcher deliberately does not translate missing flags or manufacture requests, goals, verdict identities or closing evidence. GAP rows below prevent the requested whole-workflow equivalence.

| RTS invocation / behavior | Core invocation / behavior | Result |
|---|---|---|
| `order send UNIT HH:MM FILE --fork NAME` | `order send UNIT HH:MM FILE --request REQUEST --fork NAME` | GAP: Core requires a recorded owner request. Old invocation is refused, not transparently interchangeable. |
| `send --replaces MESSAGE` | Same-holder problems listed in the body are replaced implicitly | GAP: no `--replaces`; no equivalent explicit old-message selection. |
| `send --reassign REASON` | `send --reassign REASON --request REQUEST` | Flag exists; request remains mandatory. |
| `send --clean` | `send --clean --request REQUEST` | Flag exists; request remains mandatory. |
| `send --traced FILE:LINE --mechanical` | Same flags | Supported, with the required request. |
| `send --disk` | No `--disk` | GAP: Core storage rules do not have this override. |
| `say UNIT[,UNIT] FILE [--fork NAME]` | Same spelling | Supported. |
| `problem TEXT --where FILE:LINE --to UNIT --due HH:MM --order FILE [--disk]` | `problem TEXT --to UNIT --due HH:MM --order FILE --request REQUEST` | GAP: no `--where` or `--disk`; requires request. Finding location is not a separate Todo field. |
| `hunt FILE:LINE --why REASON [--sent ORDER] [--fork NAME]` | `hunt FILE:LINE PID --why REASON (--sent ORDER | --clean) [--fork NAME]` | GAP: needs explicit problem; clean outcome must be explicit. |
| `strike UNIT --why REASON [--caught WHO] [--fork NAME]` | `strike UNIT PID --why REASON [--caught WHO] [--fork NAME]` | GAP: Core requires explicit problem, not inference from reason. |
| `done UNIT ORDER --sha SHA --repo DIR --seen FILE` | `done PID --sha SHA [--delivered ORIGINAL_SHA] [--screenshot FILE]` | GAP: no UNIT/ORDER positional selection, `--repo` or `--seen`. Check directory comes from order; `--screenshot` is for operation claims, not the old general closing evidence. |
| `done ... --sha op --seen FILE` | `done PID --sha op --screenshot FILE` | Changed spelling; explicit problem required. |
| `drop UNIT ORDER --by MESSAGE` | No `drop` | GAP. `withdraw PID --why REASON [--superseded-by PID]` closes an erroneous problem; it is not an equivalent old-order drop. |
| `close PID --works TEXT` | `close PID --works TEXT --artifact FILE` | GAP: now requires installed evidence file and a verified delivered order. |
| `fold PID --into PID --why TEXT` | No `fold`; `withdraw ... --superseded-by PID` | GAP: withdrawal is not general folding of an unassigned problem. |
| `link PID UNIT ORDER` | No `link` | GAP. |
| `board`, `next-hunt`, `overdue` | Same verb names | Commands exist. Alarm semantics are not all identical; see below. |
| `judge show [THREAD]` | `judge show` or `judge show-unit THREAD` | GAP: a thread argument to `show` is not accepted. |
| `judge ok IDS --serves QUOTE --line LINE --why REASON [--peer MESSAGE] [--fork NAME]` | `judge ok IDS --msg ORDER --serves QUOTE --line LINE --why REASON [--fork NAME]` | GAP: mandatory `--msg`; no `--peer`. Core judges only known edits on that recorded order. |
| `judge ok ... --no-owner REASON` | `judge ok-unowned ... --no-owner REASON` | GAP: different verb; `ok` rejects the old flag. |
| `judge back IDS --msg ORDER --line LINE --why REASON --roast TEXT --fix TEXT --slop KIND --fact FACT --at FILE:LINE --derive MECHANISM --by FILE:LINE [--fork NAME]` | Same spelling | Supported fields. Core posts to the verdict audience rather than the old unconditional whole-fleet broadcast; original random RTS edit IDs are not Core call IDs. |
| `judge superseded IDS --by ORDER --line LINE --why REASON` | No `superseded` | GAP: no corresponding Core verdict type or verb. |
| `judge alarm` | Same spelling | Command exists. Existing RTS verdict identities must be carried before its pending-edit count can be called equivalent. |
| `rules review [--as NAME]`, `rules answer N TEXT [--as NAME]`, `rules stale` | Same spelling | Supported; reviews live in Core's rule-review store. Old `rules.jsonl` is not imported by the requested five-file carry. |
| `capacity` | Same spelling | Command exists. Neither RTS nor Core exposes `require`/`beat` as CLI flags. Installed runtime above predates P617's external-event/count repair. |
| `rotate`, `tool_watch` | `rotate`, **`tool-watch`** | Different watcher spelling; launcher uses the hyphenated Core command. State readers use Core. |

## Alarm differences that prevent a same-alarms claim

The RTS overdue loop reports unowned problems from `problems.json`, first-call and idle-owner alarms, overdue units, stale open PRs, storage warnings and hunt due. Core derives its board from Todos and its progress owner. It also reports unfinished/no-commit work and install timing. Until all old unassigned problems, closed states, strikes and verdicts can be represented, a run on one copied Todo proves that deadline reader only, not fleet alarm equivalence.

## Carry facts that cannot be invented

* RTS `record_edit()` assigned a random ten-hex timestamp-derived ID. `verdicts.jsonl` records that ID, not the native/Core tool-call ID. Matching text is not proof of call identity.
* The frozen snapshot has 15,508 verdicts: 12,702 ok, 1,138 back, 307 superseded, 1,352 stale, 9 steer. None records a Core call ID; 13,767 name no order message. Core `OrderVerdict` requires a call ID and an assignment; only accepted/back variants exist.
* Core Todo requires a complete `ProblemOrder` even for an unassigned problem. An old problem's text/location/time does not supply an order, request, lease generation, check or evidence. Missing values must not be filled from the current thread goal or current clock.
* `closed.jsonl` includes historical closures without a checked delivery or artifact, folds, and two informal note rows. Those are not verified `InstalledProblemOrder` records.
* Some strikes explicitly have no task. They cannot be attached to an arbitrarily chosen problem.

These are missing source facts / owner model cases, not permission to keep a second writable RTS store. The temporary carry command must refuse before any Todo write when any record cannot be represented. Original files remain untouched until a complete carry is possible.

## Copied installed run, 05:10–05:12

* All six filename launchers use `current/bin/agent-comms-commander`. The supplied grep check prints 0.
* `order overdue` printed `OVERDUE: doctrine-segment (due 04:49, order d3702a707bcb, no commit since the order; no reply since the order)`. This is the earlier single-Todo copy, **not fleet alarm parity**.
* `judge show` returned seven actual native edits; `capacity` reported `covered: load 4, forks []`; `rules stale` reported the old main commander's review age. No provider input was sent.
* `rules review --as doctrine-segment` and all six `rules answer ... --as doctrine-segment` commands ran through the installed launcher; subsequent `rules stale --as doctrine-segment` reported fresh.
* Existing refactor-audit AST census parsed all 12 commander files and all 20 cutover files with zero omissions. No source scanner was added.
* `tool-watch` remained running for its eight-second observation, then its owned process group was stopped. That confirms launch, not a new-call digest or storage strike.
* `rotate` printed its P617 line, then failed: `RelationViolationError: external_transcripts.sqlite3 differs from its declaration`. This copy has the P617-converted event rows; installed T064518Z still has the old array reader. It requires the matching installed runtime, not a source splice or reversing the conversion.
* The copied carry `--apply` refused with exit 2 before any write: 336 problems, 621 strikes, 302 closure records, 15,508 verdicts. SQLite hash was unchanged. Twelve strikes name no task; two closure notes lack a complete closure identity. The existing copy has only the earlier P617 Todo, so assignment mismatch counts are **copy-specific**, not an inventory of the live Core store.
* Raw logs are under `/home/ts/.cache/agent-scratch/doctrine-segment/p617/`: `carry-copy.log`, `installed-order-0510.log`, `installed-judge-0510.log`, `installed-capacity-0510.log`, `installed-rules-0510.log`, `installed-rotate-0512.log`, `installed-tool-watch-0513.log`. No automated tests ran.

The carry implementation is unfinished: it can reconcile an already identified Core order's deadline, but refuses the historical facts above. It must not be described as a completed five-store carry. No live RTS file, live Todo, goal or monitor was changed. The old order d738a447b337 is absent from the frozen RTS deadlines; P617 names its replacement 477be5920e2e.
