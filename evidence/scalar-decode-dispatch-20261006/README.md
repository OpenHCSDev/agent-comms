# Scalar decode dispatch

FieldCodec still resolves Annotated/WireValue representations first. Exact
str/int/bool/null/float declarations now reach their existing validator before
generic typing, collection, family, dataclass and enum dispatch.

The scalar validator moved intact into the existing FieldCodec. Early dispatch
uses identity, not annotation equality. Other annotations reach the same
validator after the original structural dispatch, preserving custom metaclass
equality and record/enum construction order. Annotated null acceptance, multiple
representation refusal, strict primitive/subclass rejection, optional/union and
Literal behavior, finite-float checks and integer-to-float acceptance remain.
Encoding, schema and declaration caches are unchanged. No decoded-value cache.
validator-equality.json confirms the original validator AST is identical.

30 codec checks passed in 0.19s; two existing recursive JSON-shape checks passed
in 0.12s. New checks include a nullable int representation, bool-versus-int and
subclass refusals, invalid floats, and a record with custom scalar equality.

The original published route selected the live registry. Under its existing
shared lock and guard, one 285,219-byte / 124-thread cut was preserved privately.
The unchanged RegistryStore._decode / RegistryDocument.from_wire path was timed
including JSON parsing, alternating original and changed source for seven rounds
of twenty decodes. Original Git _decode ran on the SAME FieldCodec and store;
no second codec class or fabricated rows. Decoded documents compare equal.

Median decode: original 25.09ms, changed 20.62ms (17.8% less). Per single profiled
decode: get_origin 12,536 -> 8,730; get_args 6,268 -> 2,462;
is_dataclass 4,602 -> 796. The initial raw collector also emitted an unmatched
issubclass key as zero; that is not a measured call count and is excluded from
the committed result. Its original raw result is retained.

This is the cache-miss document path behind Registration.snapshot and Core
presentation/history readers, which supply the Toad coordination/sidebar view.
RegistryStore revision cache hits do not decode. No claim about total UI CPU,
live frame latency or duration inferred from stack-transition groups.

Raw registry cut, profiles and stdout/stderr:
/home/ts/.cache/agent-scratch/scalar-decode-dispatch-20261006/registry02/.
The first attempt against historical ~/.agent-comms refused its obsolete marker
before capture or measurement; registry01.stderr preserves it. The actual route
owner selected the current root; no guard or codec was weakened. Both operations
are terminal. No public payload, package, agent process, input or provider change.

Source evidence uses the existing refactor-audit Package parser and records
declarations and lexical calls. Other decode owners share that method name;
dynamic resolution is not inferred. No representation supply or subclass
discovery mechanism was changed. The retired 698 branch/evidence stay preserved.
