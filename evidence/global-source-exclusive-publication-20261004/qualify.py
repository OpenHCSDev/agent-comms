import hashlib,json,os,stat,sys,tempfile
from pathlib import Path
OWN=Path('/home/ts/wt/comms-cleanup-live-integration-20260929')
OP=Path('/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/native-ingress638640633-context417-receiving-20261004/new-publication436')
sys.path.insert(0,str(OWN/'tools/cutover'))
from global_extension_activation import CreateGlobalSource,ReplaceGlobalSource,ActivateGlobalExtension
from publish_retained_summary import ReviewedArtifact
ready_path=OP/'ready-receipt.json'
ready_sha=hashlib.sha256(ready_path.read_bytes()).hexdigest()
ready=json.loads(ready_path.read_text())
def require_frozen():
    assert hashlib.sha256(ready_path.read_bytes()).hexdigest()==ready_sha
    for name,sha in ready['artifacts'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==sha,name
require_frozen()
checks=[]
root_parent=Path('/home/ts/.cache/agent-scratch')
with tempfile.TemporaryDirectory(prefix='parent-global-source652-',dir=root_parent) as scratch:
    root=Path(scratch)
    source=root/'source.ts';source.write_bytes(b'\xef\xbb\xbfconst value = "\xc3\xa9";\r\n')
    def artifact(path):return ReviewedArtifact(path,hashlib.sha256(path.read_bytes()).hexdigest())
    created=root/'created.ts';new=CreateGlobalSource(artifact(source),created)
    new.require_original();new.install()
    assert created.read_bytes()==source.read_bytes() and stat.S_IMODE(created.stat().st_mode)==0o600
    checks.append('new source exact BOM/UTF8/CRLF bytes and private600 mode')
    for mode in (0o644,0o755):
        target=root/f'replace-{mode:o}.ts';target.write_bytes(b'\xef\xbb\xbforiginal\r\n');target.chmod(mode)
        before=target.read_bytes();original=artifact(target)
        replacement=ReplaceGlobalSource(artifact(source),target,original)
        preimages=root/f'preimage-{mode:o}';preimages.mkdir(mode=0o700)
        replacement.retain_original(preimages)
        assert (preimages/target.name).read_bytes()==before
        replacement.install()
        assert target.read_bytes()==source.read_bytes() and stat.S_IMODE(target.stat().st_mode)==mode
        checks.append(f'replacement/preimage exact bytes and original{mode:o} mode')
    late=root/'late.ts';late_install=CreateGlobalSource(artifact(source),late)
    late_install.require_original();late.write_bytes(b'foreign source\r\n');original=late.stat()
    try:late_install.publish_source()
    except FileExistsError:pass
    else:raise AssertionError('Creation overwrote late destination')
    assert late.read_bytes()==b'foreign source\r\n' and late.stat()==original
    checks.append('physical exclusive creation refuses late existing file unchanged')
    alias=root/'dangling.ts';alias_install=CreateGlobalSource(artifact(source),alias)
    alias_install.require_original();alias.symlink_to('absent-owner.ts');original=alias.lstat()
    try:alias_install.publish_source()
    except FileExistsError:pass
    else:raise AssertionError('Creation overwrote late dangling alias')
    assert alias.readlink()==Path('absent-owner.ts') and alias.lstat()==original
    checks.append('physical exclusive creation refuses late dangling alias unchanged')
    target=root/'original.ts';target.write_bytes(b'original');replacement=ReplaceGlobalSource(artifact(source),target,artifact(target))
    preimages=root/'conflict-preimage';preimages.mkdir(mode=0o700);held=preimages/target.name;held.write_bytes(b'held prior attempt');original=held.stat()
    try:replacement.retain_original(preimages)
    except FileExistsError:pass
    else:raise AssertionError('Preimage overwrote prior attempt')
    assert held.read_bytes()==b'held prior attempt' and held.stat()==original and target.read_bytes()==b'original'
    checks.append('original preimage refuses existing attempt without mutation')
    stale=root/'stale.ts';stale.write_bytes(b'first');original=artifact(stale);replacement=ReplaceGlobalSource(artifact(source),stale,original);stale.write_bytes(b'changed')
    try:replacement.install()
    except RuntimeError:pass
    else:raise AssertionError('Replacement admitted changed original')
    assert stale.read_bytes()==b'changed'
    checks.append('changed original replacement refused')
    operation=ActivateGlobalExtension((new,replacement),root/'activation-preimages')
    assert operation.recovery_paths()==frozenset((created,stale))
    checks.append('existing stopped installation owns exact source recovery footprint')
assert not Path(scratch).exists()
require_frozen();assert not (OP/'publication-receipt.json').exists()
result={'state':'actual private filesystem source-publication controls PASS','checks':checks,'native_inputs':0,'provider_calls':0,'public_actions':0,'package_writes':0,'frozen436_artifacts_unchanged':len(ready['artifacts']),'frozen_ready_sha256':ready_sha,'scratch_retired':True,'scope':'Original source declaration and real exclusive filesystem writes; no stopped-owner restart/public activation/UI claim'}
(OWN/'.artifacts/global-source-exclusive-publication-20261004/qualification.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
