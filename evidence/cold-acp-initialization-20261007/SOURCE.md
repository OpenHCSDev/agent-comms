# Cold ACP attachment

The current default selected `runtime-session-focus/bin/python` (Core4745).
The retained private replacement fixture was used with its authentic SDK-authored
saved question/answer. One original private worker was started explicitly; no
prompt, provider request, public owner or public default was operated on.

Ordinary cold initialize took 1.716789s; session/load took 0.094552s. A separate
profiled cold attachment took 2.938977s and 0.114584s. Both ACP parents exited0;
the worker was stopped through OwnerLifecycle. Original timings accidentally
computed `not identity.alive` instead of calling the method. The original file
is retained; direct process absence is recorded separately and the control is
corrected. No attachment was repeated to repair that metadata.

The profile shows one PrivateNkLaunch.validate -> cohort_foreground._preflight
-> native_pi._trusted_package -> native_package.verify_native_package call.
Its 21,276-node full tree traversal took 1.273s under profiling. Imports overlap
across the graph and are not additive: the ACP SDK schema construction costs
0.627s inclusive, package initialization 0.653s, dataclass generation 0.660s.
These are profiled costs, not the ordinary initialize breakdown.

## Change through the existing verifier

`package_tree_digest` now converts its root once with os.fspath, walks string
paths using os.path.join and reads original metadata with os.lstat. It removes
repeated Path construction/conversion. Both before/after path inode checks,
opened descriptor checks, complete file content, sorted names, ownership/mode,
hardlink policy and entry/byte/depth limits are unchanged. No cache, signature,
trust waiver, startup side path, native package mutation or new representation
was added. Public function signatures and record/digest spelling remain equal.

The complete source consumer search covers native launch/preflight, compaction
writer/preparation, token helpers, cutover publishers and assembly/resource
sharing scripts. They retain the same verifier. `_trusted_package` still checks
lexical ancestry before verification. Mutable shared files and symlinks still
refuse. No API consumer migration is needed for this internal traversal change.

One same-interpreter comparison on the actual retained4b package measured
installed traversal 0.822682s/CPU0.817359s and changed source traversal
0.540166s/CPU0.536826s. Both returned the exact existing pinned tree digest.
This single ordered comparison is supporting evidence, not an installed startup
speed claim or a distribution. The native package/pin/bootstrap stayed untouched.

Sixteen existing checks passed: reproducibility, unpinned byte/mode/member
changes, unsafe filesystem shapes, inventory bounds, directory-size refusal and
mutation during a read. No mock startup/protocol/provider was substituted.
Imports remain a real additional startup cost; no safe omission from their
complete declaration/consumer graph has been established. Publication of a
matched Core wheel and actual cold attachment with the changed verifier remain
integration work; no build/install was done here.

Raw results:
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007/timings.json`
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007/cold-acp.pstats`
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007/tree-check-comparison.json`
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007/joined-process-readback.json`
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007-operator/operands.json`
- `/home/ts/.cache/agent-scratch/mendel-cold-acp-20261007-operator/terminal.json`
