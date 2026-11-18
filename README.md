# anywayd
[![Made with Textual](https://img.shields.io/badge/Made%20with-Textual-blue)](https://github.com/Textualize/textual)

A simplistic background process manager for Linux.

Start a command, close your terminal, and come back later to see whether it's still running, what it printed, and how it exited. anywayd keeps running processes out of your shell's hands: a root daemon spawns and tracks them, and a small CLI/TUI talks to it over D-Bus.

## How it works

- **`anywayd`** is the daemon. It runs as root, owns the `com.anywayd.daemon` name on the system bus, spawns processes (optionally as another user, via a privilege-dropping exec helper), and records each one in a SQLite database at `/var/lib/anywayd/anywayd.db`.
- **`anyway`** is the client. Give it a command to start a process, or run it with no arguments to open a TUI for browsing and managing processes.
- stdout and stderr of every process are written to `/var/log/anywayd/<uuid>/`, owner-only (`0600`).
- Records from previous boots are detected via the boot id and cleaned up; stale entries are marked dead.

## Usage

```bash
nix run github:TotalyEnglizLitrate/anywayd
```

Or add it to your flake as an input
```nix
inputs.anywayd.url = "github:TotalyEnglizLitrate/anywayd";
```

### NixOS

```nix
{
  imports = [inputs.anywayd.nixosModules.default];

  programs.anywayd = {
    enable = true;
    package = inputs.anywayd.packages.${pkgs.system}.default;
  };
}
```

This installs the package and enables the `anywayd` systemd service. Set `programs.anywayd.systemd.enable = false` to skip the service. The package also installs the D-Bus policy and shell completions (bash, zsh, fish) for `anyway`.

### Starting a process

```bash
anyway [OPTIONS] [COMMAND]
```

| Option | Description |
| --- | --- |
| `-d`, `--cwd` | Working directory (default: `.`) |
| `-u`, `--run-as` | User to run as (default: the caller) |
| `-e`, `--env KEY=VALUE` | Set an environment variable (repeatable) |
| `--inherit-env/--no-inherit-env` | Start from the caller's environment (default: inherit) |

```bash
$ anyway "sleep 600"
Started process 3f2c8a1e-...
$ anyway -d ~/project -e RUST_LOG=debug "cargo run --release"
$ anyway -u nobody --no-inherit-env "python -m http.server"
```

`COMMAND` is split shell-style, but not run through a shell, so pipes and redirects need an explicit `sh -c`.

### TUI

Run `anyway` with no command to open the TUI. Select a process to view its stdout/stderr logs, or send it a signal:

| Key | Action |
| --- | --- |
| `k` | Kill (SIGKILL) |
| `t` | Terminate (SIGTERM) |
| `i` | Interrupt (SIGINT) |
| `s` | Pick another signal to send |
| `esc` | Close a dialog |

### Running the daemon manually

```bash
sudo anywayd [--debug]
```

Without NixOS you also need to install `com.anywayd.daemon.conf` into `/etc/dbus-1/system.d/` (or `/usr/share/dbus-1/system.d/`) so non-root users may talk to the daemon.

## Requirements

- Linux with systemd-style system D-Bus
- Python ≥ 3.12, not a free-threaded build
- The daemon must run as root
