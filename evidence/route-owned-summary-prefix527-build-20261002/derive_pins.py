from pathlib import Path
import hashlib,importlib.util,json,stat
wt=Path('/home/ts/wt/comms-task-aware-native-bundle-20261002');out=wt/'.artifacts/route-owned-summary-prefix527-20261002';root=out/'fresh-unverified/node_modules/@earendil-works/pi-coding-agent';old=wt/'stack/.pi-native-9f12ddf4e22d6d7a/node_modules/@earendil-works/pi-coding-agent'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(p):return {str(x.relative_to(p)):digest(x) for x in p.rglob('*') if x.is_file()}
a=inventory(old);b=inventory(root);assert a.keys()==b.keys()
changes=[{'path':p,'original_sha256':a[p],'new_sha256':b[p]} for p in sorted(a) if a[p]!=b[p]]
proof=json.loads((wt/'evidence/route-owned-summary-prefix-20261002/source-patch-provenance.json').read_text())
for entry in proof['files']:
 assert a[entry['path']]==entry['original_sha256'],entry
 assert b[entry['path']]==entry['after_sha256'],entry
expected={e['path'] for e in proof['files']}|{'dist/core/compaction/agent-comms-source.js','dist/core/compaction/agent-comms-source.d.ts','dist/core/session-context.js','dist/modes/rpc/rpc-mode.js'}
assert {x['path'] for x in changes}==expected,changes
for source,target in [('native-compaction-source.mjs','dist/core/compaction/agent-comms-source.js'),('native-compaction-source.d.ts','dist/core/compaction/agent-comms-source.d.ts'),('native-session-context.mjs','dist/core/session-context.js')]:assert (wt/'stack'/source).read_bytes()==(root/target).read_bytes()
spec=importlib.util.spec_from_file_location('native_package',wt/'src/agent_comms/native_package.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
manifest=wt/'stack/pi-native.sha256';original=manifest.read_text();(out/'original-manifest.sha256').write_text(original)
lines=[]
for line in original.splitlines():
 if line.startswith(m.TREE_PREFIX):lines.append(m.TREE_PREFIX+m.package_tree_digest(root))
 else:
  prior,path=line.split('  ',1);lines.append(digest(root/path)+'  '+path)
manifest.write_text('\n'.join(lines)+'\n')
receipt={'source':'f0861a2140a43c68a6313b4cc7a69c639d21fe54','manifest':digest(manifest),'tree':m.package_tree_digest(root),'capture':str(root),'canonical_package':str(wt/('stack/.pi-native-'+digest(manifest)[:16])/'node_modules/@earendil-works/pi-coding-agent'),'changed_members':changes,'all_other_files_identical_to_qualified9f12':True,'regular_files':len(b),'source_copy_bindings_exact':True,'pristine_stock_recipe':'PASS;95lockedoffline dependencies','provider_calls':0,'public_changes':False,'native_inputs':0,'qualification':'Source artifact; canonical preparation and Singer527 configured selected-route admitted prefix journey follows; NOT qualified fallback'}
(out/'pin-derivation.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt,indent=2))
