from pkg import relationships
class WireRevision:
    relationships: int
    value = relationships.RelationshipStore
    def read(self):
        return self.value
