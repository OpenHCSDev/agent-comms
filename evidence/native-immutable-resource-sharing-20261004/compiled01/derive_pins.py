from pathlib import Path
import hashlib,json,importlib.util,subprocess,stat
wt=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002');out=Path(__file__).resolve().parent;root=out/'fresh-unverified/node_modules/@earendil-works/pi-coding-agent';old=wt/'stack/.pi-native-2ea0a4d4ff2e3628/node_modules/@earendil-works/pi-coding-agent'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(root):
 result={};total=0
 for p in root.rglob('*'):
  info=p.lstat();assert not p.is_symlink(),p
  if stat.S_ISREG(info.st_mode):
   assert info.st_nlink==1,p
   if root.name == 'pi-coding-agent' and 'fresh-unverified' in str(root): assert not info.st_mode & 0o222,p
   result[str(p.relative_to(root))]=digest(p);total+=info.st_size
  else:assert stat.S_ISDIR(info.st_mode),p
 return result,total
assert root.is_dir() and old.is_dir(), (root,old)
before,oldbytes=inventory(old);after,size=inventory(root)
assert before.keys()==after.keys(),'Unexpected package member changes'
changes=[{'path':p,'original_sha256':before[p],'new_sha256':after[p]} for p in sorted(after) if before[p]!=after[p]]
expected={row['path'] for row in json.loads((out/'changed-members.json').read_text())}; assert len(expected)==1
assert {row['path'] for row in changes}==expected,changes
for name in ('dist/agent-comms-imports.json','agent-comms-extensions/global-agent-comms/index.mjs'):assert before[name]==after[name],name
spec=importlib.util.spec_from_file_location('native_package',wt/'src/agent_comms/native_package.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
manifest=wt/'stack/pi-native.sha256';original=(out/'original-manifest.sha256').read_text();assert manifest.read_text()==original
tree=module.package_tree_digest(root);rows=[];paths=set()
for line in original.splitlines():
 if line.startswith(module.TREE_PREFIX):continue
 prior,path=line.split('  ',1);paths.add(path);rows.append(after[path]+'  '+path)
# Update the existing compiled import-fence pin; whole-tree commitment covers all resources.
for path in sorted(expected-paths):rows.append(after[path]+'  '+path)
rows.append(module.TREE_PREFIX+tree);manifest.write_text('\n'.join(rows)+'\n')
sha=digest(manifest);canonical=wt/('stack/.pi-native-'+sha[:16])
receipt={'source_checkpoint':'4b65a9474401b648d84845630e8ad17ac24b1608','builder_source':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'manifest':sha,'tree':tree,'canonical_package':str(canonical/'node_modules/@earendil-works/pi-coding-agent'),'changed_members':changes,'all_other_members_equal_immutable610_2ea':True,'import_manifest_and_global_extension_unchanged':True,'regular_files':len(after),'payload_bytes':size,'new_donor_regular_files_single_links':True,'new_donor_regular_files_readonly':True,'stock_recipe':'Fresh pristine0.85.1; original source SHA fence and reviewed patch order,95lockednpm dependencies offline','single_stock_assembly':True,'promotion':'Sealed independent resources renamed into manifest-derived target; original prepare-pi-native verifies it. Sharing receiver uses only this NEW same-fence donor.','provider_calls':0,'public_changes':False,'qualification':'Artifact-only capture; compiled receiver sharing and SDK read qualification pending. No installed/public/UI/model/prompt/wholeturn/S4 claim.'}
(out/'pin-derivation.json').write_text(json.dumps(receipt,indent=2)+'\n');assert not canonical.exists(),canonical
(out/'fresh-unverified').rename(canonical)
print(json.dumps(receipt,indent=2))
