# Toad Comms in Termux

Paste into Termux:

```sh
curl -fsSL https://raw.githubusercontent.com/OpenHCSDev/agent-comms/main/tools/install-termux.sh | bash
```

Then open it:

```sh
toad-comms
```

The installer installs the native Android dependencies and the maintained Toad
stack into `~/.local/share/toad-comms`. It does not install a Linux container,
replace system Python, start agents, configure SSH hosts, or change Tailscale.
Native dependencies may take several minutes to compile on the first install.
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
