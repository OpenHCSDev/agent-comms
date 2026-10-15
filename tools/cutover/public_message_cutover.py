"""Install the public index at OwnerLifecycle's existing all-idle restart boundary."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import ClassVar

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV
from publish_runtime import RuntimePublication
from retained_index_cutover import RetainedIndexCutover


@dataclass(frozen=True)
class PublicMessageIndexCutover(RetainedIndexCutover):
    directory: Path
    writer_script: ClassVar[str] = 'public_message_writer.py'
    installer_script: ClassVar[str] = 'public_message_index.py'

    @property
    def operation_arguments(self) -> tuple[str, ...]:
        return (str(self.directory),)

    def after_stopped(self, lifecycle) -> None:
        super().after_stopped(lifecycle)
        RuntimePublication.installed().publish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--original-python', type=Path, required=True)
    parser.add_argument('--root-id', required=True)
    parser.add_argument('--native-package', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    publication = RuntimePublication.installed()
    os.environ.update(
        AGENT_COMMS_ROOT=str(args.root),
        PATH=f'{publication.runtime / "bin"}:{os.environ["PATH"]}',
        VIRTUAL_ENV=str(publication.runtime),
        **{ROOT_ID_ENV: args.root_id, PACKAGE_ENV: str(args.native_package)},
    )
    operation = PublicMessageIndexCutover(
        args.original_python, args.root_id, args.native_package, args.directory)
    results = Comms(args.root).owners.restart_owners(cutover=operation)
    print(json.dumps(FieldCodec.encode(results)), flush=True)


if __name__ == '__main__':
    main()
