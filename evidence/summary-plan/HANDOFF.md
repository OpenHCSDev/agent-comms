# Selected summary follows the native compaction plan

Real installed PR95 acceptance reached a selected summary but returned UNKNOWN
on history requiring more than four provider calls. Existing native regression
explicitly expected that larger history to fail. The wrapper's fixed total of
four calls conflicts with native compact()'s map/reduction plan; four is the
normal maximum parallel worker count, not a valid bound on total segments.

Remove that duplicate total-call constraint. The native algorithm still owns
segmentation, bounded concurrency and shrinking reductions. The selected slot
retains 90-second deadline, 1 MiB source, cumulative 256 KiB output, no retry,
exact route capture and join-before-release. UNKNOWN is never replay permission.
No provider or model changed. The wrapper calls only the captured selected stream.

All42 real native protocol/fake-provider cases pass. Larger case now summarizes
using7calls; existing3call case passes and oversizedsource is declined before
any call. Normal prepare-pi-native reproduces the updated complete-tree pin.
Actual configured-provider repeated retention run is pending; not claimed passed.
Parent will finish actual acceptance and deploy the paired Python/native package.
