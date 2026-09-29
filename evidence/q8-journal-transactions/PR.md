## Ready: whole journal transaction ownership

991 production lines deleted/replaced;1111 added (including moved declarations,
net+120). Source `8ac16539`, current main373/374 integrated. Full ownership and
receipts: [README](evidence/q8-journal-transactions/README.md).

CompactionJournal now owns only the canonical database/schema/transaction/fsync
lifetime. Production and test callers use actual operations, summaries,
private_inputs and publications owners. Former journal methods and imports are
deleted, with no compatibility reexports, second store or codec subclass.

Actual factoring beyond moves: declaration-derived enrollment exclusion set;
one state-derived unresolved query; lifecycle-owned original blocking; exact-row
CAS for every selected transition; shared raw prewrite/write exclusion; atomic
native outcome/publication persistence and exact publication acknowledgement.
Identical refusal stays idempotent and blocking. Terminal grants still mint only
after returned commit+fsync. Native/source/UNKNOWN and no-replay fences retained.
SQL schema and durable shapes unchanged.

## Verification

-94 focused journal/fresh/admission/guard cases passed,2 optional skips (13.76s).
-Current-main noneditable installed native/ACP:2 passed (22.94s), including
  publication before original binding, two continued saved-session input cycles,
  and disconnected UNKNOWN without replay. Earlier4 native cases passed33.94s,
  including correction refusal after actual native commit.
-Eleven publication race/uncertainty cases passed in retained aggregate receipt.
  Its sole failed obsolete mocked-stream/invalid-session case was deleted; its
  ordering assertion now executes in the real native journey above. All RED
  receipts retained and explained, not described as green suites.
-Ratchet: no increases;206 journal class excess lines and2 foreign-state probes
  removed. Journal82 lines; largest new transaction owner310. Ruff/diff/caller
  checks pass. Latest authoritative NRA/refactor-audit applied.

Parent owns merge/install/live. No default/launcher/live data change or native
package copy. Own env/test scratch cleaned after process-reference check. CI
is deferred. HistoryViews373 and selected lifetime367 remain separate ownership;
367 received direct API migration contract. No diagnosed scope blocker remains.
