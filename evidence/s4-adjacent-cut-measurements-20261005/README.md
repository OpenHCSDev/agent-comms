# S4 adjacent checkpoint measurements

## Working source

The recorded runner compared the last two supplied cuts. With frozen rounds r1/r2/r3 and no original r2, it placed the r1-to-r3 revision result on r3 without saying the expected r2-to-r3 measurement was unavailable.

RecallScenario.revision_intervals now owns all frozen adjacent pairs. Missing checkpoint pairs keep unavailable results; the wider original observed interval stays visible separately. ScoredScenario and the recorded runner use this same declaration rather than their supplied-subset denominators.

RecordedNativeCheckpoint.compare_acquired owns the existing distinct ancestor check, retained-source difference and authored-lineage revision measurement. Its standalone inspect and the recorded runner both borrow already corroborated captures. Both original copies of those decisions are removed. This changes no source authority, wire/schema, lifecycle or provider.

## Source coverage and remaining validation

Existing NRA SourceModuleBatchParser parsed 739 tracked Python modules under src/tests/tools/experiments, zero omissions. Before sites include original checkpoint inspect, trajectory observer, scorer, paired/CLI and configured journey consumers. This is lexical source evidence, not dynamic resolution proof. Native/JavaScript unchanged and not scanned as Python. No new scanner, runtime owner or store.

This draft is not Ready. After the coherent family change, one authored batch will check missing middle/first/final pairs, complete pairs, known contradictory intervals and shared original comparison refusal. No old original/session/SDK/provider/holder read is required or authorized. Full S4, configured comparative arms and USD75/30-pair study remain unfinished/unapproved. Original #664 qualification stays frozen; the old installed 5f wheel does not equal current main #666.
