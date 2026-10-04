import json,sys
from pathlib import Path
sys.path.insert(0,"/home/ts/wt/nra-skill-publish-20261002/skills/refactor-audit/scripts")
from audit.repository import Repository
from audit.findings import Package
repo=Repository(Path.cwd());rev=sys.argv[1]
subjects={"GlobalSourceInstall","ReplaceGlobalSource","CreateGlobalSource","ActivateGlobalExtension","VerifiedGlobalExtension","write_original","_atomic_write_text","retain_original","install","publish_source"}
result={"revision":rev,"collector":"original refactor-audit Package/Repository/FunctionFacts","roots":{},"owner_functions":{},"related_functions":[],"dynamic_limits":"Original syntactic FunctionFacts records callee names, not runtime receiver/dispatch. Historical wrappers source-read; no complete dynamic external caller proof."}
for root in ("src/agent_comms","tests","tools"):
    package=Package.load(repo,rev,root);assert not package.unparsed,package.unparsed
    result["roots"][root]={"parsed":len(package.modules),"omissions":list(package.unparsed)}
    for path,functions in package.functions.items():
        for f in functions:
            data={"declaration":f.qualname,"calls":sorted(f.constructed),"key_reads":{k:sorted(v) for k,v in f.keys.items()},"dispatch":{k:sorted(v) for k,v in f.dispatch.items()}}
            if path=="tools/cutover/global_extension_activation.py":result["owner_functions"].setdefault(path,[]).append(data)
            elif f.constructed.intersection(subjects):result["related_functions"].append(data)
Path(sys.argv[2]).write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps({"roots":result["roots"],"related_declarations":len(result["related_functions"])}))
