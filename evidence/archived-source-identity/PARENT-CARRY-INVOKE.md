# Parent-only one-use original history carry invocation

Prepared after merged Core477/Toad268 acceptance. No public command below was run.
Use the reviewed target reader only after its installation and the existing
OwnerCutover/StoppedOwnerInstallation all-stopped capability proves quiet custody.
Parent owns that batch, operator choice, installation and atomic publication.
No parallel restart/cutover mechanism is added by this receipt.

## Inputs, audit and protected originals

Source manifest:
`/var/tmp/agent-comms-live-20260927-wzjtqhza/history_sources.json`.
Archive roots:

1. `/var/tmp/agent-comms-live-20260927-wzjtqhza/history/source-0njqz198`,
   original wire_root_id `b8ff29c8e11f47f1a59b568650e580a3`,104 declarations/nine aliases.
2. `/var/tmp/agent-comms-live-20260927-wzjtqhza/history/source-a7v0vr4w`,
   original wire_root_id `aaed93d6297f4c15bccc68c211066c7e`,seven declarations/no aliases.

Suggested fresh audit directory:
`/home/ts/.cache/agent-scratch/parent-history-provenance-cutover-20261001`.
An existing receipt/directory requires inspecting its disposition, not replaying
the command. Retain exact manifest, both registry/bus_meta and derived checkpoint
preimages under source-specific subdirectories, preserving uid/gid/mode/timestamps
and recording SHA256/size/stat/root_id/source revisions. Preserve the reviewed
operator bytes/hash and invocation. Protect all original bus/wire, registry,
native journals/.input-proof/UNKNOWN/frozen audience and goal/read/route facts.
The existing native session references remain exact; do not copy or rewrite them
into a candidate path. The retained public gate/protection receipt supplies the
original source/NRA hashes; no fresh provider or prompt is needed.

## Existing original-writer -> target installer boundary

The new `_store_lock` validates schema before yielding; it cannot lock either
original four-column DeliverySources checkpoint with the five-column reader.
Use the parent's reviewed ORIGINAL-SCHEMA interpreter and the existing
`tools/cutover/retained_index_writer.py` -> `install_retained_index.py` mechanism
inside its stopped-batch hook. The writer accepts:

```text
ORIGINAL_WRITER_PYTHON retained_index_writer.py ARCHIVE_ROOT TARGET_PYTHON install_retained_index.py ORIGINAL_ROOT_ID
```

There is one invocation for each declared source above. This is not the public
main-root restart command: parent supplies the selected old-schema writer inside
the existing quiet operation. The exact original-reader prefix remains parent's
retained launch authority; a newer default/installed04 prefix is NOT a substitute
for this old-schema writer. Do not create a legacy reader or cloned package.

The original writer obtains/verifies its existing bus certificate first. Before
its marker/derived-DB transition, the release hook must preserve the preimages
above and original determining facts. It clears only checkpoint_version/seal,
retires only the preimaged derived DB, and passes its SAME opened descriptor via
pass_fds. The target's require_retained_writer checks lock inode/root_id and
calls the existing installer `_bus_locked=True`. No target lock reacquisition,
fresh independent flock, integer adapter or bypassed verification is allowed.
RetainedIndexCutover's existing audience proof stays at the public stopped-batch
owner; do not invoke a second archive owner batch or choose owners by names/PIDs.

After each installation, compare all noncheckpoint WireMetadata fields exactly,
bus/registry file revisions/hashes, source identity,111 original records/nine
aliases and referenced native/proof facts. Use the canonical current certified
reader. The original bus_meta HASH legitimately changes only because the new
checkpoint seal/version changes; retain its original preimage and field-level
transition receipt. The provenance tool's before/after bus_meta hashes cover its
read-only projection AFTER reset, not a claim that the reset left old metadata
bytes unchanged. Failure denies reads; retain its receipt and do not replay UNKNOWN.

## Exact provenance projection (separate fresh output)

The known accepted interpreter below is installed04/Core52c. Parent may use its
reviewed combined release interpreter implementing the same merged declaration,
recording that absolute interpreter, source/native pins and matching receipts.
This projection writes only the two fresh audit files; it does not publish them.

```sh
/home/ts/.cache/agent-scratch/mendel-archived-source-477-20261001/installed04/bin/python \
  /home/ts/wt/comms-archived-source-identity-20261001/tools/cutover/carry_history_provenance.py \
  --manifest /var/tmp/agent-comms-live-20260927-wzjtqhza/history_sources.json \
  --output /home/ts/.cache/agent-scratch/parent-history-provenance-cutover-20261001/history-sources-candidate.json \
  --receipt /home/ts/.cache/agent-scratch/parent-history-provenance-cutover-20261001/provenance-carry.json
```

Assert the original manifest SHA still matches the preimage and projection
receipt,104+7/nine+zero facts preserved, determining digests equal, and every
descriptor field except the newly required provenance unchanged. Parent atomically
publishes candidate bytes through the existing store publisher under its quiet
custody. Final installed opening/history acceptance is the parent release gate;
the isolated copied-index/physical04 evidence does not claim public activation.

After that publication, retire the outside-src executable from maintained tools;
retain reviewed exact audit bytes/hash/receipts/preimages. No in-package translator,
fallback, compatibility alias, extra codec or source-state mirror remains.
