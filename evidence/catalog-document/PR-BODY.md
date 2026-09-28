## R2 complete catalog ownership

- Replace the four-file mutable catalog/cache with CatalogDocument + existing A8 LockedStore; one-way lossless migration preserves preferences, audiences and saved views.
- Migrate all core callers, coherent publication/view revisions and wire→catalog mutations. Delete old-writer coexistence paths and catalog facade/state.
- Derive Message decoding from FieldCodec, preserve saved IDs/timestamps/provenance, delete mention decoder and display forwarding API, migrate CLI consumers.
- Merged main190; retain R4 OwnedTurn changes. Parent owns paired Toad and activation.

Focused local evidence:97 channel/history;135 boundary/publication/export;40 corrected revision/message cases;66 merged consumer cases (overlapping). NRA79detectors complete,0findings on targeted owners. No CI gate, provider calls or live mutations.

Full caller/deletion map, exact failed/corrected receipts, Toad contract and migration procedure: `evidence/catalog-document/HANDOFF.md`.
