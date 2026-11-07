# Friction, with the receipt

Tristan, 2026-11-07: "Are you finding a lot of friction in the system here? It's okay for you
to complain and point things out rather than just be complacent" and "any friction should be
noted and probably should be a priority since it's preventing you from doing your work
properly."

Every entry is a thing that stopped work, the exact command, and what it printed. Nothing here
is a feeling about the codebase; each one cost a turn. Ordered by what it blocked, not by what
it annoys.

## 1. The team skill's whole Orders section names a command family that does not exist

`src/agent_comms/skills/comms-team/SKILL.md` (lines 64, 87, 104, 105, 121) tells every
teammate, at every start and every compaction, to use:

```
order send UNIT ORDER.json
order problem ORDER.json --to UNIT --where PATH:LINE
order check PID --check "repo :: cmd -> N" --why "..."
order done UNIT PROBLEM
order hunt FILE:LINE --why ... --sent PROBLEM
order strike UNIT PID FILE:LINE --why "..."
order fire UNIT --why
order unstrike PID --why
```

```
$ agent-comms order --help
{"error":"argument command: invalid from_name value: 'order'; caused by
 ArgumentError: argument command: invalid from_name value: 'order'; caused by
 UnknownFamilyMember: Unknown CliCommand name: 'order'","type":"failed"}
$ ls /home/ts/.local/bin | grep -x order
                                  (nothing)
```

The declared surface is the `CliCommand` family in `agent-comms --help`, and it has no `order`
member; the actual capabilities live in the tool surface (`comms_order`, `comms_todo_create`,
`comms_order_done`, `comms_order_check`, `comms_todo` for strikes and releases, `comms_verdict`
for a send-back that strikes). So the skill restates a vocabulary nothing declares: MEMB-1,
a roster standing in for a family, and it is the highest-traffic document in the repo.

**Owner:** the skill. **Fix:** name the tools that exist, in the one place that already owns
the tool list; do not add an `order` verb to Core to match a doc.

**Why it is first:** this is the documented first step for every teammate, and it fails for
all of them. The workaround (reading `order_tools.py` to find the real tool names, then
hand-writing typed JSON) is not something the skill asks anyone to do.

## 2. Two gates each name the other as the exit

To fix a Toad bug I needed the repository. That path is:

```
$ git clone … https://github.com/OpenHCSDev/toad.git           # in scratch
StorageRule: git clone and git worktree add, move and remove are forbidden;
run agent-comms checkout add PROBLEM or checkout remove PROBLEM, which own the
pinned submodules

$ agent-comms repository-checkout add toad-tab-backlog OpenHCSDev/toad --from comms-teammate
{"error":"Unknown todo: toad-tab-backlog.","type":"failed"}
```

The refusing rule names `checkout add PROBLEM`; `checkout add` refuses because there is no
`PROBLEM`; the only way to make one is `comms_todo_create` with a typed
`CreateProblemGoalAction` (problem, repo, text, order[scope/check/edits/banned/report/run],
request, review_at, assigner, owner), which entry **1** does not document.

This is the same defect as #4305 in the goal gate, which I fixed this week: a refusal that
names an exit which also refuses is a deadlock check, and the reader cannot tell it from the
failure it guards. The repair is the same one: name the fact that would be legal and who owns
it. Here it should say "no problem row exists; create one with `comms_todo_create`" and print
the fields it needs, the way `FieldCodec` lists every bad field at once.

**Owner:** `StorageRule`'s clone refusal and `checkout add`'s unknown-todo refusal.
**Fix:** have the clone refusal name the exact call that creates the missing allocation, with
its shape.

## 3. Reading another repository needs a write allocation

`git clone` is forbidden outright, under any path, including scratch, so a read-only trace of
another repo's tests is impossible without a board row. The code already models the distinction
— `RepositoryReads()` is passed through the checkout rules as the read view — but the read path
never consults it: allocation is required before reading, not before writing.

Reading another repo to answer "does this bug have a test" is the cheapest possible way to
avoid writing the wrong fix, and today it is the most gated operation in the system.

**Owner:** the storage rule's clone ban. **Fix:** allow a scratch clone under
`checkout_base`/scratch with `RepositoryReads()` only, no edit boundary, no allocation.

## 4. The deal audit's shape cannot be discovered, and it gates every tool

`/home/ts/.cache/agent-scratch/comms-commander-2/compaction-deal.audit.json` is required
before any tool call after a compaction, and its schema exists only in the gate's prose:

- `owner_words[].words` must be verbatim (a `spine.md`-style quote is rejected, a recorded
  prompt line works);
- `meant[]` answers must open yes/no and run at least 12 words;
- `probes[]` is a tagged union on `"probe"`: `prior` needs `prior`/`words`/`refuted_by`,
  `hunt` needs `pattern`/`fact`/`at`/`derive`/`by`, `flinch` needs `answer`.

I wrote that file three times and was refused twice before anything ran, and the refusals named
the *content* that was missing ("no prior probe: name a prior…") but never the *shape*. Since
the gate blocks every tool, this cost is paid at every compaction by every session.

**Owner:** the rule gate. **Fix:** on refusal, print the missing key and its declared shape,
the way `FieldCodec` does, so one write is enough.

## 5. An unknown command is reported as a bad caller name

```
$ agent-comms order --help
{"error":"argument command: invalid from_name value: 'order'","type":"failed"}
```

`order` is not a caller; it is the command. The diagnosis points at a parameter that was never
supplied, and the true cause (`UnknownFamilyMember`) is the fifth clause of the message. A
reader fixing this looks at caller names first — I did.

**Owner:** the CLI adapter's argument decoding. **Fix:** decode the command family member
before the optional `--from`, so an unknown verb reports `UnknownFamilyMember` alone.

## 6. A read-only shell loop is refused for using a variable

```
$ for p in /home/ts/code/projects/*/; do git -C "$p" remote -v | grep -qi toad && echo "$p"; done
StorageRule: use a literal storage destination, not '$p'
```

The rule rejects path-looking variables in commands, which is right for writes and wrong for a
read-only scan over several checkouts. It also tells you to do something impossible ("use a
literal destination") for a loop whose whole point is that there is no single destination.

**Owner:** the storage rule's command scan. **Fix:** exempt commands that write nothing, or name
the one write it thinks it found.

## What is not friction

The parts that worked and should not be touched: `FieldCodec` decoding a whole JSON file and
listing every bad field at once; `agent-comms tool --tool … --arguments …` as a uniform
adapter; `comms_problem_number` handing out a free number without a guess; the install
relocking Toad/Textual/ui-theme from their branches so no relock PR is needed; and
`py-spy dump` for attribution without instrumentation.
