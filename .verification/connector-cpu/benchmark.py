import json, subprocess, time
from pathlib import Path
from agent_comms.projects.work_branches import WorkBranches
rows=[]
for repository in ("/home/ts/code/projects/agent-comms", "/home/ts/wt/codex-textual-keys"):
    path=Path(repository)
    head=subprocess.check_output(("git","-C",repository,"rev-parse","HEAD"),text=True).strip()
    for phase in ("warmup","measured"):
        begin=time.perf_counter()
        result=WorkBranches(path).commits_since(head,head)
        rows.append({"repository":repository,"head":head,"phase":phase,"seconds":time.perf_counter()-begin,"commits":len(result)})
        assert result == ()
        print(json.dumps(rows[-1]),flush=True)
