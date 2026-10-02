"""Retain the original failed subprocess cause at the cutover effect boundary."""
import subprocess


def run_cutover_child(arguments, *, environment, packet, descriptors=()):
    try:
        return subprocess.run(arguments, input=packet, env=environment,
                              pass_fds=descriptors, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as error:
        error.add_note('Original cutover child stderr:\n' + error.stderr)
        raise


def restore_stopped_batch(stopped):
    """The acquired launch selects its original decoder and RAM/OFD transport."""
    import json
    from pathlib import Path
    import agent_comms.owner_restart
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_lifecycle import OwnerRestartResult

    source = stopped.handoff.owners[0].launch
    result = run_cutover_child([
        source.interpreter, str(Path(__file__).with_name('restore_stopped_owners.py')),
        str(Path(agent_comms.owner_restart.__file__).parent), str(stopped.wire.descriptor),
    ], environment=source.environment, packet=json.dumps(FieldCodec.encode(stopped.handoff)),
        descriptors=(stopped.wire.descriptor,))
    return FieldCodec.decode(tuple[OwnerRestartResult, ...], json.loads(result.stdout))
