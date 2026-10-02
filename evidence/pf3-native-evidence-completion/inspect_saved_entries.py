"""Read saved rows without replay or private content in the receipt.

This checks decoding, not private-file/live-event authority. Reading each file's
initial byte extent permits observing an active append without chasing its tail.
"""

import hashlib
import json
from pathlib import Path

from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextProof


def inspect():
    roots = [
        Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),
        Path('/var/tmp/agent-comms-live-20260927-6_d_vdul'),
    ]
    paths = set()
    for root in roots:
        paths.update((root / 'native-sessions').glob('*/*.jsonl'))
    registry = json.loads((roots[0] / 'registry.json').read_text())
    for name in ('agent-comms-ux', 'pr95-selected-pi-summary-owner'):
        paths.add(Path(registry['threads'][name]['session_file']))
    results = []
    for path in sorted(paths):
        before = path.stat()
        rows = users = proofs = 0
        failures = []
        with path.open('rb') as stream:
            remaining = before.st_size
            while remaining:
                line = stream.readline(remaining)
                if not line:
                    raise RuntimeError('Saved source truncated while reading')
                remaining -= len(line)
                rows += 1
                try:
                    raw = json.loads(line)
                    entry = NativeEntry.from_evidence(raw)
                    if raw.get('type') == 'message' and raw['message'].get('role') == 'user':
                        users += 1
                        content = entry.message.content
                        represented = (
                            [part.to_wire() for part in content]
                            if isinstance(content, tuple) else content
                        )
                        assert represented == raw['message'].get('content')
                        assert entry.message.input_id == raw['message'].get('inputId')
                        assert entry.message.input_digest == raw['message'].get('inputDigest')
                except (ValueError, TypeError, KeyError, AssertionError) as error:
                    failures.append({'row': rows, 'error': type(error).__name__})
        journal = Path(str(path) + '.input-proof')
        if journal.exists():
            with journal.open('rb') as stream:
                remaining = journal.stat().st_size
                while remaining:
                    line = stream.readline(remaining)
                    if not line:
                        raise RuntimeError('Saved proof source truncated while reading')
                    remaining -= len(line)
                    proofs += 1
                    try:
                        raw = json.loads(line)
                        proof = NativeContextProof.from_journal(raw, path)
                        assert (proof.session_id, proof.input_id, proof.session_entry_id,
                                proof.request_generation, proof.llm_context_digest) == (
                                    raw['sessionId'], raw['inputId'], raw['sessionEntryId'],
                                    raw['requestGeneration'], raw['llmContextDigest'])
                    except (ValueError, TypeError, KeyError, AssertionError) as error:
                        failures.append({'journal_row': proofs, 'error': type(error).__name__})
        after = path.stat()
        results.append({
            'source_reference': hashlib.sha256(str(path).encode()).hexdigest()[:16],
            'rows': rows, 'user_rows': users, 'journal_rows': proofs,
            'bytes_at_start': before.st_size,
            'source_unchanged': (before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns),
            'failures': failures,
        })
    report = {
        'source_files': len(results), 'rows': sum(r['rows'] for r in results),
        'user_rows': sum(r['user_rows'] for r in results),
        'journal_rows': sum(r['journal_rows'] for r in results),
        'failures': sum(len(r['failures']) for r in results),
        'live_writes': 0, 'provider_calls': 0,
        'authority': 'shape and exact content/identity only; no execution or recovery grant',
        'sources': results,
    }
    print(json.dumps(report, indent=2))
    return report['failures'] != 0


if __name__ == '__main__':
    raise SystemExit(inspect())
