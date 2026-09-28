# Round 2 rules

**These rules bind every agent working on round 2, and override anything that conflicts with them**: round 1's documents, earlier versions of these plans, your own sense of caution, and any habit of leaving things "safe for now." Read this before any surface file.

Language models tend to treat a half-finished job as prudence: keep the old path "just in case," add a converter "to be safe," port every old test "for coverage." On this project that is how debt gets made. **A half-finished refactor is worse than none**, because it leaves two mechanisms where there was one.

---

## 1. No backwards compatibility

- **The code reads exactly one format for everything it stores or receives: the current one.** No dual-format readers. No renaming old keys on load (`raw.pop("old_key")`). No aliases for old names, no re-exports of moved names, no deprecated wrappers, no "compatibility entry points." No code path named or commented `legacy`, `compat`, `fallback`, `v1` or `old`. No `try: new … except: old`. No flag that keeps an old path alive.
- **Our formats change freely.** That includes our SQLite tables, our journals, the JSON our programs exchange, the wire's rows, our Python APIs, and the protocol between agent-comms and Toad. When one changes, every caller changes in the same PR. Toad changes in a lockstep PR, and the two are pinned and installed together.
- **External contracts are honored exactly:** pi's RPC stream, pi's settings and session files, ACP, the operating system, SQLite itself. Matching a format someone else owns is correctness, not compatibility.

## 2. Persisted state: hard cutover, no converters

- **Classify every store your change touches.**
  - *Runtime or derived state* (anything rebuildable from the wire, or transient: indexes, cursors, projections, runtime inputs, journals of in-flight work) **is reset** when the new version is installed.
  - *Durable history* (the wire log, goal history, owner decisions) is not changed by round 2. If a surface truly must change one, that needs an owner decision and a one-shot cutover tool.
- **A one-shot cutover tool lives in `tools/cutover/`, never in `src/`.** It runs once on the owner's install, and the surface is not done until the tool is deleted. Nothing in `src/` ever reads a pre-cutover format.
- **Cutover needs a quiet moment:** no turns or compactions in flight, owners restarted on the new version. That is acceptable. Plan for it; never write code to avoid it.

## 3. Delete aggressively

- **Replacing something means deleting it**: its code, its tests, its docs, its configuration.
- **Delete dead code on sight in your files:** unused functions, modules nothing imports or runs, commented-out code, unreachable branches, stale TODOs.
- **Report lines deleted and added.** A refactoring surface that adds more than it deletes owes a one-line reason.

## 4. Finish the job; partial is not a state

- **A surface is done only when its guards pass with zero exceptions across its files.** "Most call sites migrated," "old path kept for now," "follow-up to remove X" and "TODO: migrate the rest" all mean *not done*.
- **No new TODO, FIXME or follow-up** unless a named surface has accepted it on the wire and it is written into that surface's file.
- **Work you find outside your files goes to the surface that owns them,** by name, recorded in its file. Never leave a stub.
- **Dual paths end with one path.** Establish from evidence which path serves production today. If it is the new one, delete the old one now. If the old one still serves production, bring the new one to parity and then delete the old one, in the same surface. Never finish with both.

## 5. Tests protect behaviour, not structure

Busy work is fake work. The test suite is larger than the code it tests (70,353 lines against 58,026), so porting it faithfully would cost more than the refactor.

- **Tests of deleted code are deleted, not ported.**
- **Tests coupled to internal structure** (private functions, raw dict shapes, internal formats, calls on internal collaborators) are deleted when that structure changes. Replace one only where a real behaviour needs protecting, and at the highest level that protects it.
- **One test per family, not per member:** iterate the family's `names()` or members.
- **One new-case test per abstraction,** not one per surface per member.
- **Golden tests only for external contracts.** Pinning an internal format is compatibility by another name.
- **Never weaken an assertion to make a test pass.** A failing test of behaviour you kept means the code is wrong.
- **If updating a test costs more than deleting it and writing one higher-level test, delete it.** Expect the number of test lines to fall, and report tests deleted and added.
- **The gate is:** the full suite green on the merged tree, your surface's guards, and contract tests for any external format you touch. Nothing else.

## 6. Fake work, which does not count

Porting tests of deleted code. Golden files for internal formats. The same test repeated for each member of a family. Documentation that restates the code. Status messages beyond the one-line format. Re-verifying what did not change. Hash-freezing and other ceremony. Plans about plans. Compatibility layers "to be safe."

## 7. Guards are the definition of done

Every surface ships AST or grep guards, as tests in the suite, that make the old mechanism impossible to reintroduce: no `subprocess` outside the child-process module, no reads by column name outside the typed-table module, and so on. Guards stay forever, and the required CI check runs them (see R0).

---

## Round-1 provisions revoked by these rules

Where a round-1 document says any of the following, these rules win:

| Round-1 provision | Now |
|---|---|
| D17: keep thin delegating methods on `Comms` for Toad | No. Migrate Toad in a lockstep PR |
| S6: keep the export named constructors as a compatibility surface for Toad | Delete them; migrate Toad in lockstep |
| S4: carry `view2` exact-mode read markers across | Reset read markers |
| S3: keep today's stored strings exactly | Stored names may change; the stores are reset |
| S5: load old `owner_epochs` keys under their new names | Delete the loader and every `owner_epochs` compatibility accessor |
| R0/D19: a `# ratchet: boundary` exception | No exception mechanism |
| S12 (earlier draft): leave schemas unchanged in a first phase | Schemas are derived from row types now |
| S13 (earlier draft): keep a bare-PID path for old owner records | Relaunch owners; no bare-PID path |
| S9 (earlier draft): existing journals must read identically | Journals are reset at cutover |
