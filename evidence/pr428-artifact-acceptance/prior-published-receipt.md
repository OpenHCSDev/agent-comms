# Prior published #428 receipt

Source: https://github.com/OpenHCSDev/agent-comms/pull/428. These are prior published results, not newly executed matrix checks. The complete structured PR response is retained at the scratch path recorded in receipt.json, with its SHA256.

## Preserved verification statements

- 10 focused tests pass using this checkout's FieldCodec, including actual exporter, malformed/null/bool/array values, fixed denominators and duplicate keys.
- 20 baseline/candidate CLI score pairs plus exporter match; 11 invalid actual CLI inputs reject.

## Exact matching source and installed codec bytes

```json
{
  "tests/compaction_retention_fixture.py": "4e9a23ffd79bbbdd8b4fd441ae9bba7fadcaa99eb9bd276431865d6ff68162f9",
  "tests/test_compaction_retention_fixture.py": "2b1df9d4138bbb4c0db97d02dbf2747146352489ae177842e93746aacded9a74",
  "installed/agent_comms/field_codec.py": "92b8c04cf60629f1d6918b7f7d922e0a081c7389e1afe6e83a97c063eb5f1f77"
}
```

The scorer/test files match the merged artifact, and the codec path above identifies the current installed dependency. All three hashes occur in the original published receipt.
