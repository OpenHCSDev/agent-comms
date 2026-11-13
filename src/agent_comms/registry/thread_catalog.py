from pathlib import Path

from agent_comms.cli_verbs.cli_result import ThreadLaunchSettings, ToolCatalogResult
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.threads import Thread, ThreadCatalogFile
from agent_comms.storage.locked_store import PatternFile, RuntimeFile
from agent_comms.storage.store_files import AtomicFile
from agent_comms.transport.tool_declaration import NativeRequestTools


class ToolCatalogFile(RuntimeFile, PatternFile):
    """The native tool catalog every thread shares, in a file named by its own content.

    The tools are a fact of the declarations, the same for every thread, so they are encoded
    once for the declarations that exist and a launch only asks whether that file is there.
    A file with this name can hold nothing else, so it is never compared and never rewritten.
    """

    __slots__ = ()

    filename = "native-sessions/comms-tools-{digest}.json"

    @ClassFact
    def body(cls) -> str:
        return FieldCodec.compact(ToolCatalogResult(NativeRequestTools.catalog).to_wire())

    @ClassFact
    def digest(cls) -> str:
        return TextDigest.of(cls.body).value

    @classmethod
    def publish(cls, root: Path) -> Path:
        path = cls.path_in(root, digest=cls.digest)
        if not path.exists():
            AtomicFile(path).write_text(cls.body)
        return path


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
            settings = ThreadLaunchSettings(declared.service_tier, launch.arguments())
            body = FieldCodec.compact(settings.to_wire())
            if not path.exists() or path.read_text() != body:
                AtomicFile(path).write_text(body)
            return path

    @classmethod
    def launch_flags(cls, thread: Thread, root: Path) -> tuple[str, ...]:
        """What a native launch hands its extension: this thread's settings, then the tools."""
        return ("--comms-catalog", str(cls.publish(thread, root)),
                "--comms-tools", str(ToolCatalogFile.publish(LaunchRoot.from_path(root).path)))
