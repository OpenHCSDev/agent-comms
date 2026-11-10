from pkg.catalog_source import RelationshipCatalogSource


class WireRevision:
    relationships: int
    value = RelationshipCatalogSource
    def read(self):
        return self.value
