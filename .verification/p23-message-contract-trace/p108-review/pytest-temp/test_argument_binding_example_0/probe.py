import ast
from nominal_refactor_advisor.lexical_scopes import FunctionBindingProjection
from nominal_refactor_advisor.declaration_dependencies import FunctionParameterBinding
node = ast.parse('def f(pos, /, normal, *args, keyword, **kwargs): pass').body[0]
left = FunctionBindingProjection.from_function(node)
right = FunctionParameterBinding(node).without_binding()
assert left == right
print(sorted(left))
