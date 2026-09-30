"""Real installed Toad/ACP/native waits with only the localhost provider controlled."""
import argparse
import asyncio
from importlib.resources import files
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import time

from agent_comms.comms import Comms
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_package import verify_native_package
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.threads import Thread
from toad.agent_schema import AgentDefinition
from toad.app import ToadApp
from toad.widgets.transcript_history import TranscriptHistory

from l0a_native_installed_pilot import until, response_painted
from runtime_fixture import stop_test_children, stop_test_owners
from saved_state_user_journey_pilot import submit_editor


class ObservedApp(ToadApp):
    CSS_PATH = files("toad").joinpath("toad.tcss")

    def __init__(self, *args, **kwargs):
        self.wait_frames = []
        super().__init__(*args, **kwargs)

    def _display(self, *args, **kwargs):
        result = super()._display(*args, **kwargs)
        frame = "\n".join(strip.text for strip in self.screen._compositor.render_strips())
        if "Waiting for" in frame or "Applying native callback" in frame or "Receiving model" in frame:
            self.wait_frames.append({"monotonic_ns": time.monotonic_ns(), "text": frame})
        return result


async def run(arguments):
    stage = arguments.stage.absolute()
    assert stage.is_relative_to("/home/ts/wt")
    stage.mkdir(mode=0o700, exist_ok=False)
    package = arguments.package.absolute()
    verify_native_package(package)
    project, config = stage / "project", stage / "pi"
    project.mkdir(); config.mkdir(mode=0o700)
    posts, connections = [], set()
    provider_end = asyncio.Event()

    async def provider(reader, writer):
        task = asyncio.current_task(); connections.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = json.loads(await reader.readexactly(length))
            count = len(posts)
            record = {"request": count, "dispatch_received_ns": time.monotonic_ns(),
                      "input_bytes": len(json.dumps(body)), "kind": "delayed_headers" if count == 0 else "blocked_subscriber"}
            posts.append(record)
            assert count < 2, "Unexpected provider retry or input replay"
            if count == 0:
                await asyncio.sleep(2)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain(); record["headers_written_ns"] = time.monotonic_ns()
            text = "WAIT_NATIVE_OK_" + str(count) + " "
            for index in range(80 if count == 0 else 700):
                content = text + "native controlled response " * (1 if count == 0 else 32)
                packet = {"id": "wait-" + str(count), "object": "chat.completion.chunk",
                          "created": int(time.time()), "model": "fixture",
                          "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}
                writer.write(("data: " + json.dumps(packet) + "\n\n").encode())
                await writer.drain()
            packet["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            packet["usage"] = {"prompt_tokens": 300, "completion_tokens": 500, "total_tokens": 800}
            writer.write(("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode())
            await writer.drain(); record["stream_written_ns"] = time.monotonic_ns()
            if count == 1: provider_end.set()
        finally:
            writer.close(); await writer.wait_closed(); connections.remove(task)

    server = await asyncio.start_server(provider, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    (config / "models.json").write_text(json.dumps({"providers": {"wait-local": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
        "apiKey": "local-only", "models": [{"id": "fixture", "name": "Wait fixture",
        "contextWindow": 2000000, "maxTokens": 8192}]}}}))
    (config / "auth.json").write_text(json.dumps({"wait-local": {"type": "api_key", "key": "local-only"}}))
    (config / "settings.json").write_text(json.dumps({"compaction": {"enabled": False},
        "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}}}))
    service = Comms(stage / "wire", private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    environment = dict(os.environ, AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id, AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_NATIVE_CONFIG_DIR=str(config), PI_CODING_AGENT_DIR=str(config),
        AGENT_COMMS_AGENT_MODELS="wait-local/fixture", AGENT_COMMS_AGENT_BIN="pi",
        AGENT_COMMS_AGENT_ARGS="--provider wait-local --model fixture --no-extensions --no-skills --no-context-files",
        XDG_CONFIG_HOME=str(stage / "config"), XDG_STATE_HOME=str(stage / "state"),
        XDG_DATA_HOME=str(stage / "data"), AGENT_COMMS_DEBUG_LOG=str(stage / "acp.log"),
        AGENT_COMMS_RUNTIME_ROOT=str(Path(sys.executable).parent),
        TOAD_TEST_ATTEMPT=stage.name,
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    for name in ("PYTHONPATH", "PI_PROMPT", "PI_TASK", "PI_AGENT_ID", "PI_PARENT_ID", "AGENT_COMMS_STARTUP_INPUT_KEY"):
        environment.pop(name, None)
    os.environ.clear(); os.environ.update(environment)
    service.registry.declare(Thread("wait-native", frozenset(), str(project),
        model="wait-local/fixture", thinking_level="off"))
    definition = AgentDefinition.decode({"name": "Native request wait", "identity": "wait",
        "short_name": "wait", "protocol": "acp",
        "run_command": {"*": shlex.join([sys.executable, "-m", "agent_comms.acp"])}})
    app = ObservedApp(agent_data=definition, project_dir=str(project), agent_session_id="wait-native")
    slow_writer = None
    observers = []
    observer_tasks = []
    first_live_text = asyncio.Event()
    report = {"complete": False, "posts": posts, "public_mutations": 0, "paid_calls": 0}

    async def subscribe():
        owner = service.registry.require("wait-native")
        reader, writer = await asyncio.open_unix_connection(socket_path(service.root, owner.pid), limit=8*1024*1024)
        writer.write((json.dumps(SubscribeRuntimeRequest(thread="wait-native").to_wire()) + "\n").encode())
        await writer.drain()
        packets = []
        while True:
            line = await asyncio.wait_for(reader.readline(), 30)
            assert line, "Attachment closed before its canonical saved snapshot"
            packet = json.loads(line)
            assert "error" not in packet, packet
            packets.append(packet)
            if "ready" in packet: break
        return reader, writer, packets

    async def consume(reader, packets):
        while line := await reader.readline():
            packet = json.loads(line)
            packets.append(packet)
            if "WAIT_NATIVE_OK_1" in packet.get("update", {}).get("content", {}).get("text", ""):
                first_live_text.set()

    async def temporary_busy(reader):
        await first_live_text.wait()
        await asyncio.sleep(.05)
        reader._transport.resume_reading()
    try:
        async with app.run_test(size=(120, 38)) as pilot:
            view = app.selected_session.conversation
            await until(pilot, lambda: view.agent is not None and view.agent_ready, 60)
            print("ATTACHED", view.agent.session.connected, view.agent.session_id, flush=True)
            app.save_screenshot(str(stage / "startup.svg"))
            assert view.agent.session.connected, "Actual ACP attachment failed before input"
            for index in range(2):
                if index == 1:
                    reader, slow_writer, slow_packets = await subscribe()
                    slow_writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    reader._transport.pause_reading()  # Real transport backpressure, no protocol/state substitution.
                    fast_reader, fast_writer, fast_packets = await subscribe()
                    busy_reader, busy_writer, busy_packets = await subscribe()
                    busy_reader._transport.pause_reading()
                    observers.extend([fast_writer, busy_writer])
                    observer_tasks.extend([asyncio.create_task(consume(fast_reader, fast_packets)),
                                           asyncio.create_task(consume(busy_reader, busy_packets)),
                                           asyncio.create_task(temporary_busy(busy_reader))])
                await submit_editor(pilot, view.prompt.prompt_text_area, f"New isolated request {index}: reply once")
                await pilot.pause(.1)
                app.save_screenshot(str(stage / f"submitted-{index}.svg"))
                await until(pilot, lambda: len(posts) >= index + 1, 60)
                if index == 1:
                    await asyncio.wait_for(provider_end.wait(), 30)
                    # Keep the observer genuinely non-reading throughout the
                    # original native completion, rather than resuming it at an
                    # arbitrary provider-write time while publication continues.
                    await until(pilot, lambda: not service.registry.require("wait-native").executing, 90)
                    report["completed_with_nonreading_observer"] = True
                    reader._transport.resume_reading()
                    await asyncio.wait_for(consume(reader, slow_packets), 30)
                    report["nonreading_observer_retired"] = True
                    slow_writer.close(); await slow_writer.wait_closed(); slow_writer = None
                await until(pilot, lambda: not service.registry.require("wait-native").executing, 90)
                assert app._exception is None, app._exception
                await until(pilot, lambda: response_painted(app, view, f"WAIT_NATIVE_OK_{index}"), 30)
                assert view.agent.session.connected, "Fast controller lost its original attachment"
                app.save_screenshot(str(stage / f"answered-{index}.svg"))
            native = Path(service.registry.require("wait-native").session_file)
            rows = [json.loads(line) for line in native.read_text().splitlines()]
            originals = [row for row in rows if row.get("message", {}).get("role") == "user"]
            assert len(originals) == 2 and len(posts) == 2, "Original input or provider call replayed"
            attempts = InputDispositions(service.root / InputDispositions.filename).read().rows
            assert len(attempts) == 2 and all(row.has_started and not row.unresolved for row in attempts.values()), "Subscriber loss changed an accepted input's disposition"
            report["original_dispositions"] = [row.public_status for row in attempts.values()]
            observations = [json.loads(line) for path in (service.root / "diagnostics").glob("*.requests.jsonl")
                            for line in path.read_text().splitlines()]
            assert observations, "No retained request measurements"
            requests = {}
            for observation in observations:
                assert observation["native_process"] is not None, "Native request clock lost its original process fence"
                requests.setdefault(observation["native"]["requestId"], []).append(observation)
            assert len(requests) == 2, "Unexpected provider request or replay"
            request_groups = sorted(requests.values(), key=lambda group: group[0]["native"]["startedAtMs"])
            for group in request_groups:
                stages = {point["native"]["stage"]: point["native"] for point in group}
                for stage_name in ("dispatch", "headers", "first_event", "first_delta_consumed", "stream_end", "finished"):
                    assert stage_name in stages, f"Original native transport omitted {stage_name}"
                assert stages["headers"]["status"] == 200
                assert int(stages["dispatch"]["monotonicNs"]) <= int(stages["headers"]["monotonicNs"]) <= int(stages["first_event"]["monotonicNs"]) <= int(stages["first_delta_consumed"]["monotonicNs"])
            delayed_stages = {point["native"]["stage"]: point["native"] for point in request_groups[0]}
            report["native_delayed_headers_ms"] = delayed_stages["headers"]["elapsedMs"] - delayed_stages["dispatch"]["elapsedMs"]
            assert report["native_delayed_headers_ms"] >= 1900, "Provider wait was not measured at original transport boundaries"
            report.update(native_originals=len(originals), observations=observations,
                          visible_wait_frames=len(app.wait_frames), provider_calls=len(posts))
            for label, packets, task in (("fast_reader", fast_packets, observer_tasks[0]),
                                         ("temporarily_busy_reader", busy_packets, observer_tasks[1])):
                assert not task.done(), f"{label} was incorrectly retired"
                text = "".join(packet.get("update", {}).get("content", {}).get("text", "") for packet in packets)
                assert "WAIT_NATIVE_OK_1" in text, f"{label} missed the native reply"
                report[label] = {"packets": len(packets), "reply_observed": True}
            reader, writer, packets = await subscribe()
            writer.close(); await writer.wait_closed()
            report["recovered_attachment_packets"] = len(packets)
            snapshots = [update for packet in packets
                         for update in decode_updates(packet.get("update", {}).get("_meta"))
                         if isinstance(update, TranscriptSnapshotUpdate)]
            assert len(snapshots) == 1, "Recovery did not publish one original canonical snapshot"
            snapshot = snapshots[0]
            original = service.transcripts.capture_page_read("wait-native")
            assert snapshot.identity.same_content(original.identity), "Recovery used a different source/frontier"
            source_text = "\n".join(event.text for event in snapshot.page.events if hasattr(event, "text"))
            assert "WAIT_NATIVE_OK_1" in source_text, "Saved native reply missing after recovery"
            report["recovered_snapshot_bytes"] = len(json.dumps(FieldCodec.encode(snapshot)).encode())
            assert report["recovered_snapshot_bytes"] > 64 * 1024, "History fixture did not exceed the normal socket high water"
            report["recovered_identity"] = FieldCodec.encode(snapshot.identity)
            assert len(posts) == 2, "Attaching replayed an original native input"
            (stage / "frames.json").write_text(json.dumps(app.wait_frames))
            report["complete"] = True
    except BaseException as error:
        report["complete"] = False
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for writer in observers:
            writer.close()
        for task in observer_tasks:
            task.cancel()
        await asyncio.gather(*observer_tasks, return_exceptions=True)
        if slow_writer is not None:
            slow_writer.close(); await slow_writer.wait_closed()
        await asyncio.to_thread(stop_test_owners, service.root)
        await stop_test_children(stage.name)
        report["cleanup"] = "fixture-owned children retired"
        server.close(); await server.wait_closed()
        await asyncio.gather(*tuple(connections), return_exceptions=True)
        (stage / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"complete": report["complete"], "posts": len(posts), "stage": str(stage)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
