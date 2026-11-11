"""Observe the real publication owner without replacing the store's lock behavior."""

import json

from p23_revision_owner_plan import ROOT, patch, stage

print(json.dumps({"stages": [stage(
    "p23-observe-coordinator-publication-owner",
    "The original consumer run reached 426 passes and exposed an old os.link observer whose signature rejects the publication owner's src_dir_fd/dst_dir_fd keywords before setting its event. Observe the selected Platform.publish_new_file method instead, forwarding its complete original call. Keep the closed-stage-before-publication, missing-target, transaction-lock and reopen assertions unchanged. This also follows the actual no-replace publication owner on platforms that do not use hard links.",
    [patch(ROOT / "tests/test_coordination.py", [
        ("from agent_comms.execution.child_process import SpawnedProcess",
         "from agent_comms.execution.child_process import Platform, SpawnedProcess"),
        ('''    before_link = threading.Event()
    publish = threading.Event()
    original_link = os.link
    errors = []

    def paused_link(source, destination, *, follow_symlinks=True):
        if destination == path:
            before_link.set()
            assert publish.wait(15)
        return original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", paused_link)''',
         '''    before_publish = threading.Event()
    publish = threading.Event()
    platform_type = type(Platform.current())
    original_publish = platform_type.publish_new_file
    errors = []

    def paused_publish(platform, source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if destination == path:
            before_publish.set()
            assert publish.wait(15)
        return original_publish(platform, source, destination,
                                src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(platform_type, "publish_new_file", paused_publish)'''),
        ("        assert before_link.wait(15)", "        assert before_publish.wait(15)"),
    ])],
)]}, indent=2))
