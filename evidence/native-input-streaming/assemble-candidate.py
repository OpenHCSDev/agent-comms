from pathlib import Path
import re,shutil,subprocess
root=Path('/home/ts/wt/comms-native-session-entry-store-20260928')
source=root/'.artifacts/native/node_modules/@earendil-works/pi-coding-agent'
candidate=Path('.artifacts/pi')
patch=Path('stack/native-session-storage.patch').read_text()
for rel in re.findall(r'^--- a/(.+)$',patch,re.M):
 if rel=='dist/core/agent-session.js': continue
 if (source/rel).exists():
  shutil.copyfile(source/rel,candidate/rel)
for a,b in [('native-session-entry-store.mjs','session-entry-store.js'),('native-session-context.mjs','session-context.js'),('native-compaction-source.mjs','compaction/agent-comms-source.js')]:
 target=candidate/'dist/core'/b;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(Path('stack')/a,target)
# Apply Darwin's disjoint context methods to our generated proof/claim owner.
parts=re.split(r'(?=^--- a/)',patch,flags=re.M)
part=next(p for p in parts if p.startswith('--- a/dist/core/agent-session.js\n'))
subprocess.run(['patch','--batch','--fuzz=0','-p1','-d',str(candidate)],input=part.encode(),check=True)
