"""Run the source acquisition owner inside a real installed runtime, without patching it."""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from types import CodeType, FrameType, FunctionType
from typing import Annotated, cast

from agent_comms.field_codec import FieldCodec, PathText


@dataclass(frozen=True)
class AcquisitionSample:
    path: Annotated[Path, PathText]
    seconds: float
    tree_hash_calls: int
    content_reads: int


@dataclass
class PackageTrace:
    digest_code: CodeType
    tree_hash_calls: int = 0
    content_reads: int = 0

    def __call__(self, frame: FrameType, event: str, argument: object) -> None:
        # CPython owns these external profiling events; compare function identities.
        if event == "call" and frame.f_code is self.digest_code:
            self.tree_hash_calls += 1
        elif event == "c_call" and argument in (io.open, os.open, os.read):
            self.content_reads += 1


def measure_acquisition(source: Path) -> AcquisitionSample:
    from agent_comms.native_package import VerifiedNativePackage, pinned_native_package

    # Resolve the installed artifact before loading the changed source owner.
    root = pinned_native_package()
    namespace = runpy.run_path(str(source))
    owner = cast(type[VerifiedNativePackage], namespace["VerifiedNativePackage"])
    digest = cast(FunctionType, namespace["package_tree_digest"])
    trace = PackageTrace(digest.__code__)
    sys.setprofile(trace)
    started = perf_counter()
    try:
        package = owner.acquire(root)
        seconds = perf_counter() - started
    finally:
        sys.setprofile(None)
    return AcquisitionSample(package.path, seconds, trace.tree_hash_calls, trace.content_reads)


if __name__ == "__main__":
    print(json.dumps(FieldCodec.encode(measure_acquisition(Path(sys.argv[1])))))
