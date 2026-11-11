from __future__ import annotations

from nominal_refactor_advisor.lexical_scopes import LexicalScopeContext as LexicalScopeContext



class _DeclarationDependencyCollector(LexicalScopeContext):
    scopes = ('owned',)
