# Narrow codec dispatch improvement

FieldCodec still owns every encode/decode/project/schema path. The existing
parked change removes work whose answer is already known from the declaration:
plain classes have no Annotated metadata, absent encode annotation needs only
the actual value capability, and exact JSON scalars need no record/enum dispatch.
No lookup table, scalar codec, decoded-value cache or process mechanism was added.

Representation selection stays first. Annotated representations, WireValue
capabilities, nullable forms, nominal families, records, enum values, tuple/union
declarations and strict bool/int admission retain their existing owners.
issubclass is still evaluated at each representation selection, so inherited
and virtual capability changes are not frozen by a cache. Metadata ambiguity
still refuses. Unknown/custom annotations continue through the original path.

One unsafe parked shortcut was corrected before publication: tuple membership
on type(value) could call arbitrary metaclass equality before record encoding.
Exact type identity now selects the early JSON scalar branch. The existing
dynamic-record declaration control covers encoding as well as decoding.

AST tracing across Core/Toad src/tests/tools found no parse omissions. All four
representation consumers remain in FieldCodec: encode, decode, project and
value_schema. Representation declarations and call sites are retained in
/home/ts/.cache/agent-scratch/mfc01/codec-dispatch/owner-consumers.txt.

All 30 existing codec controls passed in 0.23s, including Annotated/text/null,
strict primitives, arrays, records, families, projection and schema checks.
The actual captured 81-owner registry was decoded and encoded through its
original RegistryDocument.from_wire/FieldCodec owners, 300 times per source.
Original CPU: 5.9718s; changed CPU: 4.9285s (17.5% lower). Canonical outputs
were identical. These were sequential processes using the same owner module in
the finished checkout, restored after the baseline; no alternate codec was
created. The captured registry contains declarations, not native/user history.

Raw cost/checks/sample: /home/ts/.cache/agent-scratch/mfc01/codec-dispatch/.
An initial harness import refusal happened before the benchmark; corrected
source import path then ran the one before/after measurement.

This does not prove an installed wheel-latency improvement. The original
cross-thread profile does not identify main-thread blocking. Parent's newer
thread-CPU capture identifies native layout cost and has its own owner.
ChannelHistoryReader closes over live Comms/source state in its thread worker;
this change does not invent a process-transfer contract for that closure or
discard private marker/guard/snapshot checks. Parent owns affected installation
and actual UI measurement. No provider, replay, prefix mutation or build ran here.
