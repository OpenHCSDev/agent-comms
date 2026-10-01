# Declared family schemas

Parent owns this narrow FieldCodec contribution for S2 PR475. The existing
encoder/decoder already supports discriminator-selected dataclass families;
value_schema does not describe their instance values. This blocks the actual
DecisionScopeSelection tool catalog. Named tool payloads use record_schema:
their selector is already supplied by the API, outside the argument object.

Derive alternatives from the original DeclaredFamily membership and use its
declared discriminator for nested instance values. Preserve the named-payload
record_schema contract and share its existing field builder; preserve
all wire encoding, decoding, proofs and representations. No codec subclasses,
manual member roster, alternate decoder or payload format change.

Verify the codec's schema against its actual round trips and the receiving S2
tool catalog. Installed affected acceptance belongs to the paired S2 journey.
