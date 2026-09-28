"""Read copied existing native journal/history without sending or replaying input."""

import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextProof

source = Path(sys.argv[1])
source_proof = Path(str(source) + ".input-proof")
owned = Path(sys.argv[2])
receipt = Path(sys.argv[3])
owned.mkdir(parents=True, exist_ok=True)


def revision(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


before = revision(source), revision(source_proof)
with TemporaryDirectory(prefix="retained-native-", dir=owned) as temporary:
    copied = Path(temporary) / "session.jsonl"
    copied_proof = Path(str(copied) + ".input-proof")
    for original, target in ((source, copied), (source_proof, copied_proof)):
        shutil.copyfile(original, target)
        target.chmod(0o600)
    assert before == (revision(source), revision(source_proof)), "Source changed during copy"
    retained = revision(copied), revision(copied_proof)
    header, entries = NativeEntry.read_evidence(copied)
    contexts = NativeContextProof.read_history_evidence(copied, header, entries)
    assert contexts
    assert retained == (revision(copied), revision(copied_proof))
    result = {
        "installed_module": __import__("agent_comms.native_pi", fromlist=["x"]).__file__,
        "copied_session_bytes": copied.stat().st_size,
        "copied_proof_bytes": copied_proof.stat().st_size,
        "native_entries": len(entries),
        "retained_contexts": len(contexts),
        "copied_history_unchanged": True,
        "source_unchanged_during_copy": True,
        "provider_requests": 0,
        "input_replayed": False,
        "disposable_copy_removed": True,
    }
receipt.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
