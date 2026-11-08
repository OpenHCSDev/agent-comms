# Toad Comms in Termux

Paste into Termux:

```sh
apt update && apt full-upgrade -y && apt install -y curl && curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-termux.sh | bash
```

Then open it:

```sh
toad-comms
```

Toad runs locally on Android. The installer upgrades Termux, installs the
dependencies, and creates the `toad-comms` command. Your existing Tailscale app
handles the network.

With Termux:Widget installed, open the **Toad Comms** shortcut in `~/.shortcuts`.
Run the same installer command to update.

## Remote buses

Use Toad's **Open bus** menu. Automatic tailnet discovery currently requires the
desktop `tailscale status` command, which Android's Tailscale app does not expose
to Termux. The installer leaves discovery and host configuration unchanged.

The shortcut accepts Toad's normal arguments, including a saved UI profile:

```sh
toad-comms --comms-profile /path/to/profile.json .
```

## Builds and updates

Android wheels come from the [Termux User Repository](https://termux-user-repository.github.io/pypi/)
and our [native wheel cache](https://github.com/OpenHCSDev/agent-comms/releases/tag/termux-wheels).
The cache includes parsers built from complete upstream release sources, with
source and wheel checksums. Dependencies without a matching wheel compile locally.

uv resolves the stack's current compatible releases and tracked branches. pip
installs that complete result and checks the dependencies.

Build tools, source, the app environment, native locks, Cargo artifacts and pip
wheels stay in `~/.local/share/toad-comms`. uv keeps its cache in `~/.cache/uv`.
Keep both directories to reuse builds. New versions and Python ABIs need matching
wheels or a new build.

Installation log: `~/.local/share/toad-comms/install.log`.
