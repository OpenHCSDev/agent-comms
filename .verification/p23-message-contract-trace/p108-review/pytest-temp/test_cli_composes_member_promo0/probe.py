from nominal_refactor_advisor import declaration_dependencies, lexical_scopes
owner = lexical_scopes.LexicalScopeContext
consumer = declaration_dependencies._DeclarationDependencyCollector
assert owner is declaration_dependencies.LexicalScopeContext
assert consumer._resolve_name is owner._resolve_name
assert '_resolve_name' not in vars(consumer)
