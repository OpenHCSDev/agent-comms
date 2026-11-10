# Friction, with the receipt

Tristan, 2026-11-07: "Are you finding a lot of friction in the system here? It's okay for you
to complain and point things out rather than just be complacent" and "any friction should be
noted and probably should be a priority since it's preventing you from doing your work
properly."

Every entry is a thing that stopped work, the exact command, and what it printed. Nothing here
is a feeling about the codebase; each one cost a turn. Ordered by what it blocked, not by what
it annoys.

## 1. The team skill's whole Orders section omits the program, so every verb fails

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

The verbs are real, and so is the program:

```
$ agent-comms-team order --help
usage: order [-h] {send,strike,fire,unstrike,withdraw,done,pr,check,run,unlink,serve,say,
              retire,end,session-end,problem,drop,fold,link,close,hunt,next-hunt,install,idle} ...
$ agent-comms-team judge --help
usage: judge [-h] {show,alarm,ok,promote,reviewing,qualify,superseded,abandoned,dropped,back,batch,rewritten} ...
```

`pyproject.toml:66` installs `agent-comms-team = "agent_comms.team.cli:main"`; `agent-comms` is
a different binary, the JSON adapter CLI, which has no `order` member. So the skill is correct
about the verbs and silent about the program, and the silence is what fails: a teammate types
`order send …` (not on PATH) or `agent-comms order send …` (`UnknownFamilyMember`), and both
read as "this capability does not exist". The correction is one line naming the launcher, not a
rewrite of thirteen commands.

**Owner:** the skill. **Fix:** name the tools that exist, in the one place that already owns
the tool list; do not add an `order` verb to Core to match a doc.

**Why it is first:** sending an order is the documented first step for every teammate, and it
fails for all of them the same way. It also cost me the ability to open a problem for the Toad
bug, which is entry 2 — one missing program name cascades into every later refusal.

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

**Owner:** the repository-source family and storage checks. **P130 repair:** expose the
existing acquisition and `RepositoryReads()` path through the normal tool adapter:

```sh
agent-comms tool --from UNIT --tool comms_repository_read \
  --arguments '{"repository":"github:OWNER/REPO"}'
```

An existing local repository path is also accepted as `repository`. A remote read uses the
caller's existing private donor and origin's declared default branch; a local read preserves
its requested branch and bytes. Neither requires a goal, a problem, or an editable checkout.
The result carries the declared `Repo`, including its checkout path. Reading never adds that
path to the caller's editable roots, and subsequent source edits are still refused. Direct
`git clone` stays refused because it does not declare a read-only destination; its refusal
shows the actual native read call derived from the tool declaration, alongside the existing
writable-checkout instructions.

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

**Owner:** Git's effect decoding and the shell's loop-body word. **P17 reproduction:** direct
`git -C "$repo" status --short` still raises the destination refusal. On the current baseline,
the exact loop above instead passes accidentally: `do git ...` is decoded as a program named
`do`, hiding the first command's effects. The same blind spot hides a first-body `touch "$p/file"`
or `git -C "$p" reset --hard`; admission of the original loop alone is therefore not proof.

**Repair:** the working-tree effect requests physical directory resolution only when needed,
while `do` at command position introduces the real body command. Regression coverage checks
reads, first-body writes, forbidden checkout creation, redirections, and repeated literal `-C`.
With the source changes stashed: 10 failed / 9 passed. Restored with the storage suite: 94 passed.

## 7. Nothing on this host writes the frames, so every `order` verb refuses

The largest one. Every `agent-comms-team order …` verb, which is the documented way to create
or send work, refuses before doing anything:

```
$ agent-comms-team order problem ORDER.json --as comms-teammate --to comms-teammate --where …
Refused: no failure recorded, so no heartbeat is running
Tristan last completed: unrecorded
Board unavailable: team plan missing: /home/ts/.agent-comms/team_plan.json
```

The file genuinely is not there, and neither is `team_frames/`:

```
$ ls /home/ts/.agent-comms/team_plan.json
ls: cannot access '/home/ts/.agent-comms/team_plan.json': No such file or directory
$ ls -d /home/ts/.agent-comms/team_frames
ls: cannot access '/home/ts/.agent-comms/team_frames': No such file or directory
```

The chain, from the code: `ServicePasses.require_current` (`src/agent_comms/service_passes.py:101`)
raises `BoardUnavailable` unless `services.heartbeat_current(self.heartbeat)`; `TeamFrame.empty`
(`src/agent_comms/team_hud.py:325`) raises `Tristan frame missing` when a teammate's file is
absent; and the only writer is `TeamFrame.replace_all` (`team_hud.py:340`), called once from
`TeamHeartbeat` (`team_heartbeat.py:169`) — a long-running pass that runs in the commander/UI
process, not in a teammate's runtime and not in the CLI:

```
$ agent-comms heartbeat --name comms-teammate
{"heartbeat": "comms-teammate"}
$ ls /home/ts/.agent-comms/team_frames
ls: cannot access …: No such file or directory
```

`agent-comms heartbeat` only marks liveness (`src/agent_comms/cli_verbs/heartbeat.py:16`,
`ctx.threads.heartbeat(self.name)`); it writes no frame and no plan. So on a bus with no
commander attached — which is exactly every native Pi teammate — the documented order surface is
unusable, and the fallback is the native `comms_*` tools.

**Owner:** the heartbeat entry point. **Fix:** make `agent-comms heartbeat` run the same
`TeamHeartbeat` pass once (frames + plan), so the CLI surface works on a host with no UI; and
have `BoardUnavailable` say that, instead of only the missing path.

## 8. A check can only run at two revisions, so a merged tree's content is not expressible

`OrderCheck.command` is one shell line, run at the branch point and at the delivered commit
(`src/agent_comms/todos.py:268`, `BranchPointCheck`). It cannot read a third revision:
`git show origin/main:path | grep -c …` needs a pipeline, and the storage-rule shell decoder
refuses a parenthesised command outright —

```
Refused: StorageRule: the shell command cannot be decoded (A parenthesized command requires its
own command position)
```

so even `grep -c 'CatalogCommandAction(Action' src/toad/catalog_actions.py` is unwritable as a
check, and the discriminator has to be spelled `CatalogCommandAction.Action`, with `.` standing in
for the paren. That is a workaround a sender has to discover by trial, on the one line that decides
whether their merged work can ever be closed.

**Owner:** `CheckCommand` and the shell decoder. **Fix:** let a check name the revision it reads
(`--at <ref>`), or accept one `git show <ref>:<path> | …` form as a declared check shape.

## 9. A check must count the defect; nothing said so, and I called the gate broken

Two merged orders could not close, and I diagnosed a Core defect: "`order done` greps a working
tree instead of the merged head". That was wrong, and the refusal had already said so:

```
Refused: Check already prints 0 before any work: `git grep -c DeclaredFamily origin/main -- …`
proves nothing; write a check that fails now
```

The rule is in the class docstring: delivered work is checked against **the main commit it was
built on, never main since**, because once the fix merges, current main passes and proves nothing.
So a count check counts the *defect* and expects **0**:

```
branch point 5bba60760  grep -c CatalogCommandAction.Action  -> 1
merged      f6b6f53     grep -c CatalogCommandAction.Action  -> 0
```

I had written it the other way round (count the fix, expect 2), then spent a long stretch building
a fix for a bug that did not exist, and told the owner it was a Core defect. The refusal named its
rule in plain words; I read it as an obstacle instead of as the specification.

**Owner:** nothing in the code. The rule lives at `todos.py:268` and the message at
`goals.py:1455`; what is missing is that `order problem`/`order check` never states the direction
where a sender will read it. **Fix:** the check refusal should say which direction it wants —
"count the defect; expect 0" — beside the command it is refusing.

## 10. A fresh checkout starts on whatever branch its source tree had

`agent-comms checkout add P5 --from comms-teammate` produced a tree at `158cd260f`, which is not
`origin/main` (`ad8f2343` then). It inherited the branch of the registered source checkout, so it
arrived on someone else's unmerged work and imported `agent_comms.registry_document` at the top
level while main ships `agent_comms/registry/registry_document.py`. The suite then failed on
import, in a tree nobody had touched — the first thing a new checkout does is mislead about the
code it claims to be.

**Owner:** the checkout allocation (`CheckoutAllocation.declared_for_work`, `repo.Checkout.add`).
**Fix:** a new allocation starts detached at the repository's main, and says which commit it took.

## 10b. A takeover of a stopped thread's checkout is refused by a presence that disagrees with the registry

Tristan authorised taking `/home/ts/wt/codex-toad-keys` because `latency-controls` is offline.
Both paths refuse:

```
StorageRule: /home/ts/wt/codex-toad-keys is a read/launch source, not allocated editable work
{"error":"Retired owner is no longer stopped"}     # thread_status.py:154
```

while `agent-comms threads` reports `latency-controls | stopped`. The tree is clean with zero
commits ahead of main, so nothing is at risk; the takeover is refused by a presence check that
disagrees with the registry and offers no flag for "the owner is gone, the tree is free".

**Owner:** `ThreadNotStoppedError` and `RepositoryCheckoutOwner`. **Fix:** read liveness from the
same registry the verb that reports `stopped` reads, and let the owner of the bus pass a
`--take-over` that records who took it.

## 11. An unreadable triage answer costs the turn, and sometimes costs the answer too

One invalid triage reply ended a turn that had already done real work, and the prose in it never
reached the channel — which is how a claim ("Taking it") ended up existing only in a message with
one reader.

The retry exists: `RejectedTriageOutcome.close_ask` calls `stage.burn_turn(...)`
(`private_send_stage.py:298`) and `asked_again` → `reask` re-asks once
(`src/agent_comms/selected_turn.py:326-341`). In that run the claim ended `failed` with no
replacement reserved, and a peer's identical invalid answer *did* get the second offer. So the
retry is a call site that survives in one path and not the other.

**Owner:** `RejectedTriageOutcome` and the ask loop. **Fix:** reserve the replacement before
burning the turn, and keep the rejected text so a later turn can still post it.

## What is not friction

The parts that worked and should not be touched: `FieldCodec` decoding a whole JSON file and
listing every bad field at once; `agent-comms tool --tool … --arguments …` as a uniform
adapter; `comms_problem_number` handing out a free number without a guess; the install
relocking Toad/Textual/ui-theme from their branches so no relock PR is needed; and
`py-spy dump` for attribution without instrumentation.
