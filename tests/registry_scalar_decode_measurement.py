"""Compare the original and changed decoder on one canonical registry cut.

Runs original Git source through the same FieldCodec and RegistryStore, without
changing the canonical registry, a package, or its cache. No synthetic rows.
"""
import ast
import cProfile
import hashlib
import json
from pathlib import Path
import pstats
import statistics
import subprocess
import sys
from time import perf_counter

from agent_comms import field_codec
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_store import RegistryStore


def main(original_revision: str, original_path: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    source = subprocess.check_output(
        ["git", "show", f"{original_revision}:src/agent_comms/field_codec.py"], text=True
    )
    owner = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == "FieldCodec")
    original = next(node for node in owner.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_decode")
    original.decorator_list = []
    namespace = {}
    exec(compile(ast.Module(body=[original], type_ignores=[]), "original-field-codec", "exec"),
         vars(field_codec), namespace)
    original_decode = classmethod(namespace["_decode"])
    changed_decode = FieldCodec.__dict__["_decode"]
    store = RegistryStore(original_path)
    with store.locked(shared=True):
        store.private_guard_unlocked()
        payload = original_path.read_bytes()
    saved = output / "registry-original.private.json"
    saved.write_bytes(payload)
    saved.chmod(0o600)
    raw = json.loads(payload)
    methods = {"original": original_decode, "changed": changed_decode}
    results = {}
    try:
        documents = {}
        for name, method in methods.items():
            FieldCodec._decode = method
            documents[name] = store._decode(raw)
        assert documents["original"] == documents["changed"]
        elapsed = {name: [] for name in methods}
        for round_index in range(7):
            for name in (tuple(methods) if round_index % 2 == 0 else tuple(reversed(methods))):
                FieldCodec._decode = methods[name]
                start = perf_counter()
                for _ in range(20):
                    store._decode(json.loads(payload))
                elapsed[name].append((perf_counter() - start) / 20)
        for name, method in methods.items():
            FieldCodec._decode = method
            profile = cProfile.Profile()
            profile.runcall(store._decode, raw)
            profile.dump_stats(str(output / f"{name}.pstats"))
            calls = {function: sum(value[1] for (_, _, symbol), value in pstats.Stats(profile).stats.items()
                                   if symbol == function)
                     for function in ("get_origin", "get_args", "is_dataclass")}
            results[name] = {"median_seconds": statistics.median(elapsed[name]),
                             "round_seconds": elapsed[name], "profile_calls": calls}
    finally:
        FieldCodec._decode = changed_decode
    result = dict(boundary="original RegistryStore._decode including JSON parsing; cache-miss document path",
                  original_revision=original_revision, payload_sha256=hashlib.sha256(payload).hexdigest(),
                  payload_bytes=len(payload), threads=len(raw["threads"]), results=results,
                  scope="same readonly canonical cut; no cache-hit/live UI latency claim")
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
