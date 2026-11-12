# Toad in Android's native Terminal

Open Android's **Terminal** app (the Debian Linux VM, not Termux), then paste:

```sh
sudo apt-get update && sudo apt-get install -y curl && curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-toad.sh | bash
```

When it finishes, run:

```sh
toad-comms
```

`toad` is also available. Run the same installer command to update.

The installer installs Debian's build tools, uv, and the Python version declared
by our stack. It resolves the tracked Toad, Textual and other source packages,
checks the installed dependencies, and publishes the commands under
`/usr/local/bin`, so an already-open Terminal can use them immediately.
Linux ARM64 wheels work normally here; no Termux packages or Android wheel index
are used. Source builds run one at a time for small VMs.

Installation files and log: `~/.local/share/toad-comms/`. uv retains downloads in
`~/.cache/uv`. Updates keep these caches, preserve the previous resolved lock,
and stop rather than discard source edits.

Both native Terminal and [Termux](TERMUX.md) use `tools/install-toad.sh` for the
shared source update, package checks and launcher. The published Termux installer
URL remains an entry point to that same installer.

## SSH access to the VM

SSH access is optional; installing and using Toad does not require it.
Termux's SSH configuration does not transfer into the Debian VM.

Android's Terminal port-forwarding implementation listens on the **phone's
localhost**, not its Wi-Fi or Tailscale address. Enabling a forwarded port does
not make it reachable from another machine. For direct remote access without
Termux, run Tailscale inside the Debian VM itself and use its separate address.
Install `openssh-server`, add your public key to the VM user's
`~/.ssh/authorized_keys`, and connect to the port configured in that VM's SSH
server. Keep private keys on the connecting machine.
