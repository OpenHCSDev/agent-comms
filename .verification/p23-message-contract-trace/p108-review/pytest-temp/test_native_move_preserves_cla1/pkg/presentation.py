from pkg import relationships
class WireRevision:
    relationships: int
    value = lambda self: relationships.RelationshipStore
