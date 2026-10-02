"""Retain the original failed subprocess cause at the cutover effect boundary."""
import subprocess


def run_cutover_child(arguments, *, environment, packet, descriptors=()):
    try:
        return subprocess.run(arguments, input=packet, env=environment,
                              pass_fds=descriptors, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as error:
        error.add_note('Original cutover child stderr:\n' + error.stderr)
        raise
