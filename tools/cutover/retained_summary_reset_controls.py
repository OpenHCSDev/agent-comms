"""Small installed-declaration/file-custody controls, never a public cutover."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys

# Own outside-src operator code, with original installed Core/SDK declarations.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.input_disposition import InputDispositions
from agent_comms.active_route import ActiveRoute
from agent_comms.field_codec import FieldCodec
from retained_summary_reset import RuntimeCompactionReset
from publish_retained_summary import ReviewedArtifact, ReviewedRetainedSummaryCohort

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
base = args.output
base.mkdir(mode=0o700)
checks = []

def private_file(path, body):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def fds():
    return {entry.name: os.readlink(entry) for entry in Path('/proc/self/fd').iterdir()
            if entry.exists()}

root = base / 'complete'
root.mkdir(mode=0o700)
original = root / 'compaction-commits.sqlite3'
CompactionJournal(original)  # Authentic installed writer creates its own SQL.
for suffix in ('-journal', '-wal', '-shm'):
    private_file(Path(str(original) + suffix), b'original private companion ' + suffix.encode())
inputs = InputDispositions(root / InputDispositions.filename)
inputs.record('bus:160', seq=160, owner='fixture', admission=1,
              target='fixture', text='never replay')
private_file(root / 'native.jsonl', b'original sealed native bytes\n')
private_file(root / 'native.jsonl.input-proof', b'original proof bytes\n')
protected = {str(path): sha(path) for path in (root / inputs.filename, root / 'native.jsonl', root / 'native.jsonl.input-proof')}
images = {path.name: path.read_bytes() for path in RuntimeCompactionReset(root).paths}
before = fds()
receipt = RuntimeCompactionReset(root).retain_and_remove(root / 'preimages')
assert fds() == before
assert all(not path.exists() for path in RuntimeCompactionReset(root).paths)
assert all((root / 'preimages' / name).read_bytes() == body for name, body in images.items())
assert all(path.stat().st_mode & 0o777 == 0o600 for path in (root / 'preimages').iterdir())
assert protected == {path: sha(Path(path)) for path in protected}
assert inputs.read().lookup('bus:160').unresolved
checks.append('authentic SQLite journal plus three named companions retained before removal; original UNKNOWN/native/proof bytes unchanged; descriptors closed')

for case in ('wrong-mode', 'hardlink', 'symlink', 'orphan'):
    root = base / case
    root.mkdir(mode=0o700)
    original = root / 'compaction-commits.sqlite3'
    if case != 'orphan':
        private_file(original, b'original runtime bytes')
    companion = Path(str(original) + '-wal')
    private_file(companion, b'original companion bytes')
    if case == 'wrong-mode':
        companion.chmod(0o644)
    elif case == 'hardlink':
        os.link(companion, root / 'another-link')
    elif case == 'symlink':
        companion.rename(root / 'original-companion')
        companion.symlink_to(root / 'original-companion')
    members = {path: path.read_bytes() for path in RuntimeCompactionReset(root).paths if path.exists()}
    before = fds()
    try:
        RuntimeCompactionReset(root).retain_and_remove(root / 'preimages')
    except (RuntimeError, ValueError, OSError):
        pass
    else:
        raise AssertionError(case + ' accepted')
    assert fds() == before
    assert all(path.read_bytes() == body for path, body in members.items())
    checks.append(case + ': refuses before any original removal; descriptors closed')

root = base / 'complete'
before = fds()
try:
    RuntimeCompactionReset(root).retain_and_remove(root / 'preimages')
except FileExistsError:
    pass
else:
    raise AssertionError('Old attempt reused')
assert fds() == before
checks.append('existing preimages refuse reuse of the original attempt')

artifact = ReviewedArtifact(base / 'approved-artifact', '0' * 64)
cohort = ReviewedRetainedSummaryCohort(base / 'future-target', Path(sys.executable),
    base / 'current', ActiveRoute(base, 'f' * 32, base / 'native'), base / 'native',
    artifact, artifact, ())
assert FieldCodec.decode(ReviewedRetainedSummaryCohort, FieldCodec.encode(cohort)) == cohort
checks.append('reviewed cohort uses original FieldCodec/PathText roundtrip; no format alias or raw fallback')

result = {'state':'private-file-custody-controls-passed', 'checks':checks,
          'runtime_reset':receipt, 'protected_originals':protected,
          'public_actions':0, 'provider_calls':0, 'owner_starts':0, 'prompt_count':0,
          'strength':'runtime resource controls; not whole cutover or public UI acceptance'}
(base / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'state':result['state'], 'checks':len(checks), 'receipt':str(base/'receipt.json')}))
