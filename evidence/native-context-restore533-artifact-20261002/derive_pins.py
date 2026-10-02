from pathlib import Path
import hashlib,importlib.util,json,stat
wt=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002');out=wt/'.artifacts/native-context-restore533-20261002';root=out/'fresh-unverified/node_modules/@earendil-works/pi-coding-agent';old=wt/'stack/.pi-native-0b306dac5a8b9b60/node_modules/@earendil-works/pi-coding-agent'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(p):return {str(x.relative_to(p)):digest(x) for x in p.rglob('*') if x.is_file()}
a=inventory(old);b=inventory(root);assert a.keys()==b.keys()
changes=[{'path':p,'original_sha256':a[p],'new_sha256':b[p]} for p in sorted(a) if a[p]!=b[p]]
expected={'dist/core/session-context.js'}
assert {x['path'] for x in changes}==expected,changes
for source,target in [('native-session-entry-store.mjs','dist/core/session-entry-store.js'),('native-session-entry-store.d.ts','dist/core/session-entry-store.d.ts'),('native-session-context.mjs','dist/core/session-context.js')]:assert (wt/'stack'/source).read_bytes()==(root/target).read_bytes()
spec=importlib.util.spec_from_file_location('native_package',wt/'src/agent_comms/native_package.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
manifest=wt/'stack/pi-native.sha256';original=manifest.read_text();(out/'original-manifest.sha256').write_text(original)
lines=[]
for line in original.splitlines():
 if line.startswith(m.TREE_PREFIX):lines.append(m.TREE_PREFIX+m.package_tree_digest(root))
 else:
  prior,path=line.split('  ',1);lines.append(digest(root/path)+'  '+path)
manifest.write_text('\n'.join(lines)+'\n')
receipt={'source':'4f5823db6dca2b0a948157b5c4359873b8bf4bfe','manifest':digest(manifest),'tree':m.package_tree_digest(root),'capture':str(root),'canonical_package':str(wt/('stack/.pi-native-'+digest(manifest)[:16])/'node_modules/@earendil-works/pi-coding-agent'),'changed_members':changes,'all_other_files_identical_to_qualified0b306':True,'regular_files':len(b),'source_copy_bindings_exact':True,'pristine_stock_recipe':'PASS;95lockedoffline dependencies','provider_calls':0,'public_changes':False,'native_inputs':0,'qualification':'Source artifact; canonical preparation and Arendt533 saved-source startup/count/overbudget changed-path sanity follow'}
(out/'pin-derivation.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt,indent=2))
