from .lexical_bindings import LEXICAL_SCOPE_BINDING_AUTHORITY
class FunctionParameterBinding:
    def __init__(self, node):
        self.node = node
    def without_binding(self):
        return frozenset((LEXICAL_SCOPE_BINDING_AUTHORITY.argument_names(self.node)))
