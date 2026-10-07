# Reuse the publication lock's certified source

The installed saved-history capture spent 324ms waiting in flock during its
earlier 697ms profiled read. This does not identify the historical lock holder.
The transcript's before/content/after acquisitions answer independently changing
questions; none were removed or merged.

`Publisher.publish_initial_cohort` unnecessarily opened and verified a checkpoint
already acquired by its `StoreLock`. Human admission then opened it again for
its complete gap/duplicate scan. Publication now borrows that original certified
source for both decisions. Its committed prefix, marker and original file/seal
currentness checks remain. It retains the full human sequence/ID scan, exact
registry/catalog revision fence, participant publication and original durable
append/seal algorithm. No new index, cache, lock policy or timeout was introduced.

Fresh `publish_ordinary` initializes the protocol after acquiring a previously
uncertified lock. An ExitStack opens and owns its first checkpoint for this
case. Existing roots borrow the lock's source. The source's lifetime ends with
the same physical lock; no consumer retains its connection or admission permit.

Complete related callers: `Publisher.publish_ordinary` is the only supplied-lock
caller; `Messaging.send_initial_cohort` acquires its own original bus lock through
this method. Direct callers in `seed_retained_index_fixture.py`,
`shared_bus_restart_native.py`, `test_coordination_cohort.py`,
`test_private_bus_checkpoint.py`, and `test_private_human_ingress.py` retain the
same API and decision owner. Keyed responses, claims and append algorithms were
read and remain independent and unchanged. Fresh initialization is the only
production path that changes the bus between acquiring this lock and entering
initial-cohort publication; it has no acquired source and takes the fallback.

## Verification

Fifteen existing private human-ingress/checkpoint controls passed in 6.28s,
including full audience, duplicate refusal, UNKNOWN reservation gaps,
post-append UNKNOWN, separate-process serialization, checkpoint reopening and
pending-seal recovery. No new mocked controls were authored.

An additional actual private fresh-root human publication succeeded without
explicit protocol initialization. Private measurement on a sealed root recorded
checkpoint verification calls decreasing from three to one and SQLite checkpoint
connects from four to two (including the required append connection). The full
strict public scan still ran once. Small-store profiled publication times were
70.7ms installed and 74.9ms changed source: no latency improvement claim.
The installed old publisher bytes equal the determining pre-change source.
Other installed/source dependencies were not asserted globally identical.

One installed read-only `openhcs-helper` transcript capture completed in 352.5ms
with 8.55ms encoding and the same 23,027-byte payload. No attachment, prompt,
read acknowledgment, native launch or owner restart occurred. It used the
unchanged installed publisher, so its timing cannot demonstrate this change's
live effect. Historical lock-holder and the full ACP load latency remain unknown.

Raw checks, profiles, private stores and measurement scripts are retained in
`/home/ts/.cache/agent-scratch/mendel-publication-source-reuse-20261007/`.
The earlier read profile remains in
`/home/ts/.cache/agent-scratch/mendel-session-load-cost-20261007/`.
The publisher change is source-checked only; delivery requires normal integration.

## Publication audience acquisition

Established-root ordinary publication previously performed route prevalidation
through separate Registration reads, then acquired a new RegistrySnapshot and
CatalogDocument to select its audience. Publisher now validates the route from
that same acquired snapshot/catalog. Fresh unmarked roots retain prevalidation
before protocol initialization. The existing initial publisher still owns
visible sender/target, human origin, canonical incarnation, task source,
mentions, audience and wake decisions. Keyed/claim validators retain their own
acquisition paths. No snapshot crosses Messaging's identity-creation write.

Registration.snapshot and ChannelCatalog.read close their document locks before
returning. Publication's original wire/bus custody remains; before/after file
revision fences still reject registry/catalog changes before append. Certified
source, UNKNOWN sequence/ID scan, append durability and typed failure semantics
are unchanged. Root/metadata currentness checks were not removed merely because
they repeat: they answer independent admission/source facts.

All 759 src/tests/tools modules parsed without omissions; related publication
callers were enumerated. External dynamic callers are unresolved. Sixteen
existing human ingress/current delivery checks passed in 4.50s, and the existing
stale-goal-publication check passed in 0.33s. One newly authored private channel
publication retained all 20 recipients; profile records one guarded registry
read, one catalog read and one route validation. Source-derived established-root
publisher read counts are registry 3->1 for channels / 5->1 for direct messages,
and catalog 2->1 for channels. Human identity acquisition remains separate.
There is no measured live before/after latency claim.

Raw private profile and result:
/home/ts/.cache/agent-scratch/mendel-publication-audience-read-20261007/.
The first result extractor used a nonexistent WireLog.read_metadata method after
the send; corrected read-only certified acquisition inspected the original
committed row. The input was not sent again. No public/provider/native action,
installed change or worker restart occurred.
