"""One-shot historical manifest carry; immutable source artifacts are never edited.

Use only with the matching new reader. The output is explicit; there is no
public-root default or in-place update. The release owner publishes it after
quiet installation, then retires this executable.
"""

from __future__ import annotations

import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.registry_provenance import RegistryProvenance
from agent_comms.store_files import _atomic_write_text
from agent_comms.thread_provenance import ThreadProvenance


def carry(manifest: Path):
    original = manifest.read_bytes()
    raw_sources = json.loads(original)
    result, receipts = [], []
    for raw in raw_sources:
        # This is the one retired artifact boundary, outside production. Every
        # retained field is selected by its declaration, never a legacy roster.
        root = Path(raw["root"])
        registry_path = root / "registry.json"
        before = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in ("bus.jsonl", "registry.json", "bus_meta.json")}
        stored = json.loads(registry_path.read_bytes())
        declarations = {
            name: FieldCodec.decode(ThreadProvenance, {
                item.name: record[item.name]
                for item in fields(ThreadProvenance) if item.name in record
            })
            for name, record in stored["threads"].items()
        }
        provenance = RegistryProvenance(
            threads=declarations,
            aliases=FieldCodec.decode(dict[str, str], stored["aliases"]),
        )
        source = FieldCodec.decode(HistorySource, {
            **raw, "provenance": FieldCodec.encode(provenance),
        })
        source.validate()
        encoded = FieldCodec.encode(source)
        if encoded["provenance"] != FieldCodec.encode(provenance):
            raise ValueError("Original determining provenance changed during encoding")
        if {key: value for key, value in encoded.items() if key != "provenance"} != raw:
            raise ValueError("Original source descriptor changed during provenance carry")
        after = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in before}
        if before != after:
            raise ValueError("Original historical source changed during provenance carry")
        facts = FieldCodec.encode(provenance)
        facts_digest = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
        receipts.append({
            "source": str(root), "wire_root_id": source.wire_root_id,
            "declarations": len(declarations), "aliases": len(provenance.aliases),
            "original_hashes": before, "originals_unchanged": True,
            "determining_facts_before_sha256": facts_digest,
            "determining_facts_after_sha256": hashlib.sha256(
                json.dumps(encoded["provenance"], sort_keys=True).encode()
            ).hexdigest(),
            "source_revisions_unchanged": True,
        })
        result.append(encoded)
    if manifest.read_bytes() != original:
        raise ValueError("History manifest changed during provenance carry")
    return result, {"manifest": str(manifest), "original_manifest_sha256":
                   hashlib.sha256(original).hexdigest(), "sources": receipts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.manifest.resolve():
        parser.error("Carry writes a separate candidate; release publication has a single owner")
    if args.output.exists() or args.receipt.exists():
        parser.error("Candidate/receipt must be fresh; preserve previous evidence")
    sources, receipt = carry(args.manifest)
    _atomic_write_text(args.output, json.dumps(sources, indent=2) + "\n")
    _atomic_write_text(args.receipt, json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
