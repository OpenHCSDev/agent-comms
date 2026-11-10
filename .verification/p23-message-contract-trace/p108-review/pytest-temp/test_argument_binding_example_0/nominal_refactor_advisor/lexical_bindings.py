from __future__ import annotations
import ast
class LexicalScopeBindingAuthority:
    @staticmethod
    def argument_names(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> frozenset[str]:
        arguments = node.args
        return frozenset(
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        ) | frozenset(
            argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument
        )

LEXICAL_SCOPE_BINDING_AUTHORITY = LexicalScopeBindingAuthority()
