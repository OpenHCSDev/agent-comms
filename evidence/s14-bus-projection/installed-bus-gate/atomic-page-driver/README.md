# Preserved installed application driver

The original u07 driver ran the immutable installed application in st/LinuxDriver, using the existing committed SourceCapture custody helper and isolated Xvfb. Its raw run is preserved; this directory is measurement infrastructure, not product source.

`driver-provenance.json` records baseline Toad source 15601 and exact original/current file hashes. The original copied tests and CSS remain protected locally, but are not duplicated in this repository. To reconstruct them, export each `original_files` path from the recorded Toad Git object into this directory, then apply `driver-adjustments.patch` here. The patch contains only the original execution oracle, failure census, predeclared fixture-root admission and measurement bridges. It does not alter the installed product, provider/admission budgets or public route.

`admitted_frames.py` observes original CompositorUpdate emissions after the real display call. Its reviewer replays incremental ANSI per App using the existing pyte dependency and rejects duplicate actual answer lines. Synthetic body matching is a test oracle, never application deduplication.

`run_physical_bus.py` was invoked for u07 only. Do not rerun it against that original fixture or replay its inputs. The recorded failure must be followed through by the existing publication owner before another authorized fresh journey.
