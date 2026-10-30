"""Publish the native tool catalog from a current registry declaration."""

from pathlib import Path

from .cli_result import ThreadToolCatalogResult
from .field_codec import FieldCodec
from .launch_root import LaunchRoot
from .registry_store import RegistryStore
from .store_files import AtomicFile
from .threads import Thread, ThreadCatalogFile
from .tool_declaration import NativeRequestTools


class ThreadCatalog(ThreadCatalogFile):
    """The catalog writer owns acquisition through replacement, on every producer."""

    __slots__ = ()

    @classmethod
    def publish(cls, thread: Thread, root: Path) -> Path:
        launch = LaunchRoot.from_path(root)
        with RegistryStore.at(launch.path).current_entry(thread.name) as current:
            declared = current.entry.thread
            declared.require_original_project(thread)
            path = declared.catalog_path(launch.path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            catalog = ThreadToolCatalogResult(
                tuple(NativeRequestTools.catalog()), declared.service_tier, launch.arguments(),
            )
            body = FieldCodec.compact(catalog.to_wire())
            if not path.exists() or path.read_text() != body:
                AtomicFile(path).write_text(body)
            return path
