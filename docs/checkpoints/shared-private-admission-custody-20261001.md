# Shared private admission custody

Owner: Kepler. Public execution/install owner: parent. Existing failed original
`d0c838af6ca8f8f592fcc976cfdd5dd6`, nra-domain-mapping, seq273 is preserved and
must never be replayed. Current installed Corecac7 diagnostic shows nonblocking
prompt-binding directory acquisition failing after the one-use token is taken.

Scope: the original PrivateSendAdmission and its prompt-binding/sidecar/journal
exclusions. Acquire every cancellable pregrant resource before taking the token,
recording UNKNOWN or writing bytes. Hold original custody through the one raw
send; no retry after consumption, competing admission algorithm, state mirror,
registry, fallback or synthetic binding. Trace every consumer and lock order.

Acceptance: actual private Core/bus/ACP/native journey with three parallel native
owners and shared channel/sidecar contention, pregrant cancellation with zero
bytes, and exactly one original native user ID per sent input. Preserve UNKNOWN,
failed source and diagnostics. Provider-only local controls; no old user replay,
public owner change or optional476 repeat. Resource warnings require bounded
existing fixtures, no more than three independent real gates.

This draft precedes implementation. Arendt483 queue installation is independent;
Mendel's source/input ownership is unchanged. Direct peer tools are currently
absent from this task's available catalog; the dead35901 transport is not used.

## Source closure and resource ownership

Code checkpoint009236b5; continuous observer checkpointa037a296. The six changed
production files are private_send_admission.py, private_send_stage.py,
native_prompt_binding.py, locked_store.py, compaction_private_inputs.py and
compaction_journal.py. Production delta133 additions /61 deletions. Retired
reserve→release→reacquire-fence send code is deleted at the original consumer.

The original total order is wire → bus → registry → coordinator EXCLUSIVE →
prompt-binding snapshot → input disposition shared read → journal EXCLUSIVE.
Every acquisition in the raw writer is nonblocking and inside _exclusion's
pregrant Busy conversion. Its ExitStack unwinds partial custody. Only then does
the original _once token become consumed. Existing claim/owner rules remain on
NativeSendStage, RegistryOwner and ParticipantOwner; no consumer invents them.

The binding reader now delegates to one scoped expected_prompt_binding owner;
NativeSendStage.bound_prompt holds that same original sidecar while validating
the original typed binding. Recovery/history readers use the same boundary.
The sidecar's pending/unknown commit refusal is not converted to Busy or retried.
The coordinator transaction excludes bind_expected_prompt's generation writers;
nonblocking acquisition releases partial locks rather than waiting under them.

PrivateInputSend inherits the original PrivateInputs journal role and owns only
its held database, canonical source and locked InputDocument resources. Its
mark_unknown uses the original PrivateRawInput row and the journal's one durable
commit/fsync method. SQLite locking_mode=EXCLUSIVE retains the original physical
lock across that checkpoint and the next transaction until connection close.
There is no extra lock file, advisory registry, new token, mutable seen list or
post-token input/journal acquisition. Both existing reserve and send_fence users
delegate to the same resource owner. The raw writer and all postgrant uncertainty
handling are unchanged. Postgrant errors never become fresh Busy probes.

## Actual installed acceptance

The existing tests/shared_bus_restart_native.py was extended with a declaration
family for real wire/binding contention. No Core, bus, ACP or native path is
mocked. Only the localhost provider is controlled. The binding member holds the
original immutable snapshot after all actual burst bindings exist, ensuring it
tests final send admission rather than delaying an earlier binding producer.

Exactly three bounded private gates were run, serially:

- Original installed cac7/native593 RED,3 owners:18.896s,0 provider POST. The
  original Err11 in read_expected_prompt_binding reproduced. Raw failure and
  diagnostic are retained at /home/ts/wt/a484r01; all workers retired.
- Installed candidate/corea037 production, native0064,3 owners:23.736s PASS.
  The binding snapshot was physically held3s. All3 native turns were active
  before provider release;3 localhost POST and3 distinct native user entries.
  Each original coordination input ID joins exactly one native entry and its
  original durable raw marker. No admission failure or user replay.
- Same installed candidate cancellation,1 owner:16.433s PASS. A genuine raw
  writer waited at original admission; cancellation joined at0 provider POST.
  The cancelled reservation remains unchanged, with no sent generation, UNKNOWN
  marker or native user entry. A separate fresh original completed its real
  triage→full reply with2 POST and2 exact native entries. ACP emitted12 packets /
  14 typed facts. Raw source /home/ts/wt/a484c01; all workers retired.

Candidate source was installed as a wheel in the owned inactive runtime; all323
source files byte-match the published candidate. SDK0.12.1 and other unchanged
dependency artifacts are borrowed from the immutable receiving61 prefix; native
0064 trust verification passed. No PYTHONPATH/source overlay, installed-default
change, old original retry, public owner signal or paid provider call.

Committed evidence: evidence/shared-private-admission-20261001 contains the
three receipts/invocations, exact original-ID join, installed source inventory
and retained resource budget. Real SQLite pregrant input/journal refusal and
durable-checkpoint custody:3 focused tests passed. Original ownership guards:
6 passed. A subsequent correction to use the table's declared name in the
concurrent-reader assertion passed its one affected check; no native gate repeat.

Counterevidence: an earlier selected-summary/fresh-session suite reported29
failures,33 passes,2 skips; the shown fixtures omit the required retained member
introduced by475. This PR neither relaxes that declaration nor repairs another
owner's summary fixtures. The default covariance, public channel UI, full S2 and
performance targets are not claimed by these scoped admission results. Parent
owns review, merge, paired installation and public acceptance. No further native
gate is necessary without a concrete source change or remaining risk.
