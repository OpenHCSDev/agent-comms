from .lexical_bindings import LEXICAL_SCOPE_BINDING_AUTHORITY


class FunctionBindingProjection:
    @classmethod
    def from_function(cls, node):
        return frozenset((LEXICAL_SCOPE_BINDING_AUTHORITY.argument_names(node)))
