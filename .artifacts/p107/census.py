import ast, json
from pathlib import Path
from nominal_refactor_advisor.ast_tools import parse_python_module_roots, ModuleSyntaxIndex
from nominal_refactor_advisor.class_index import build_class_family_index
roots=(Path('src/agent_comms'),Path('/home/ts/wt/toad-p107/src/toad'))
modules=parse_python_module_roots(roots,use_parse_cache=False,parse_workers=1)
family=build_class_family_index(modules)
projected={(c.file_path,c.line):c for c in family.classes_by_symbol.values()}
classes=[]; sites=[]
for module in modules:
    syntax=ModuleSyntaxIndex.build(module.module)
    for _,node in syntax.indexed_nodes_of_type(ast.ClassDef):
        record=projected.get((module.file_path,node.lineno))
        classes.append({'path':str(module.path),'line':node.lineno,'name':node.name,'bases':[ast.unparse(b) for b in node.bases], 'projection':record.symbol if record else 'OPEN original syntax only'})
    for i,node in enumerate(syntax.depth_first_nodes):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Assign,ast.AnnAssign,ast.If,ast.IfExp,ast.BoolOp,ast.UnaryOp,ast.Call,ast.ImportFrom,ast.Return,ast.Attribute)):
            text=ast.unparse(node); first=text.splitlines()[0]
            if any(word in first for word in ('GoalProgress','goal.progress','revision_goal.progress','self.goal.progress')):
                sites.append({'path':str(module.path),'line':node.lineno,'function':syntax.enclosing_function_name(i),'node':type(node).__name__,'source':first})
print(json.dumps({'modules':len(modules),'parse_omissions':0,'classes':classes,'sites':sites,'limits':'Static Python binding only. Dynamic family/FieldCodec and Textual dispatch remain runtime checks; no exhaustive dynamic resolution claim.'},indent=2))
