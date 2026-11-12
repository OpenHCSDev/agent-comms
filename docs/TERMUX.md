# Toad Comms in Termux

For Android's built-in Debian VM, use the [native Terminal installer](ANDROID-TERMINAL.md).

Paste into Termux:

```sh
apt update && apt full-upgrade -y && apt install -y curl && curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-termux.sh | bash
```

Then open it:

```sh
toad-comms
```

Toad runs locally on Android. The installer upgrades Termux, installs the
dependencies, and creates the `toad-comms` command. It shares its source update,
checks and launcher with the native Terminal installer. Your existing Tailscale app
handles the network.

With Termux:Widget installed, open the **Toad Comms** shortcut in `~/.shortcuts`.
Run the same installer command to update.

## Remote buses

Open a saved bus, then use Toad's **Open bus** menu. The connected Core discovers
machines through its own Tailscale client and asks their buses for access. The
phone does not need the desktop Tailscale CLI or a hard-coded host list.

Android's Tailscale app does not expose its peer list to Termux. A fresh phone
therefore needs one initial UI profile from a bus; after connecting, discovery
runs through that bus. The menu paints immediately while discovery runs.

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
