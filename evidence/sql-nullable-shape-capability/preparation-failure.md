# First source-test failure retained

The first six-test invocation passed the existing five controls and failed the
new invalid-input assertion: it passed raw bool/string/float directly into a
mandatory nominal field. Such an object has no WireValue.to_wire contract,
so the codec correctly did not treat it as a nominal state and raised
AttributeError. The semantic row input is now a RecordedEpoch with an invalid
raw value, testing the actual owning shape boundary. ValueError remains
required for bool/string/float; no codec or product fallback was added.

Original location: test_nullable_shape_column.py:68, _Field.encode:271,
FieldCodec.encode:265, WireValue.encode:69, AttributeError:
'bool' object has no attribute 'to_wire'. Initial result1 failed/5 passed0.18s.
