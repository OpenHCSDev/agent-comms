# Toad Comms in Termux

Paste into Termux:

```sh
apt update && apt full-upgrade -y && apt install -y curl && curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-termux.sh | bash
```

Then open it:

```sh
toad-comms
```

The installer upgrades Termux's package set, then installs the native Android dependencies and the maintained Toad
stack into `~/.local/share/toad-comms`. It does not install a Linux container,
replace system Python, start agents, configure SSH hosts, or change Tailscale.
Android wheels come from the [Termux User Repository](https://termux-user-repository.github.io/pypi/),
including Maturin and Pydantic Core. Maturin must have a compatible binary wheel;
the installer does not silently compile that build tool. Other dependencies without
a matching wheel may still compile. Termux resolves current compatible releases and
tracked branches from the stack's declared constraints rather than retaining older
desktop lock selections. uv resolves the entire dependency graph; pip installs that
export without resolving it again, then checks the installed dependencies.
Previous generated native locks are retained before updating the source checkout.
Cargo uses one build job by default and keeps
a shared build cache for dependency resolution and installation.
Build tools remain installed. The source, virtual environment, Cargo target cache
and pip wheel cache remain under `~/.local/share/toad-comms`; uv also retains its
cache under `~/.cache/uv`. Keep those directories to reuse compatible builds.
Changed dependency versions or a changed Python ABI may still require rebuilding.
Run the same command again to update. Installation output is also saved in
`~/.local/share/toad-comms/install.log`.

An optional Termux:Widget entry named **Toad Comms** is installed in
`~/.shortcuts`. It works if you already have the Termux:Widget app installed;
otherwise just use the command above.

Toad runs locally, including its layout, scrolling and keyboard input. Its
existing **Open bus** menu selects remote buses. This installer does not alter
bus discovery or remote authorization. Tailscale must already be connected.
Android's Tailscale app does not provide the desktop `tailscale status` CLI,
which the current automatic discovery uses; being on the VPN alone does not
make that CLI available to Termux. This remains a separate discovery limitation,
not something this installer resolves by hard-coding hosts.

The shortcut forwards Toad's normal command-line arguments. For example, an
existing UI profile can be opened with:

```sh
toad-comms --comms-profile /path/to/profile.json .
```

Phone-only keyboard, touch and Android build behavior should be checked on the
phone. Host-side checks are not a claim that those interactions were tested.
