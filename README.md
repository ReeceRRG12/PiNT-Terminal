# PiNT Terminal

A Python desktop terminal for macOS and Windows, using the PiNT Desktop navy and teal design language. Version 0.2.0 organises the vault as groups containing devices, with shared group credentials and optional custom credentials per device.

## Downloads

[Download the latest stable release](https://github.com/ReeceRRG12/PiNT-Terminal/releases/latest) or [view version 0.2.0](https://github.com/ReeceRRG12/PiNT-Terminal/releases/tag/v0.2.0).

- **macOS Apple Silicon:** open `PiNT-Terminal-0.2.0-macOS-arm64.dmg`, drag `PiNT Terminal.app` to Applications, then open it.
- **Windows:** download and run `PiNT-Terminal-0.2.0-Windows-x64.exe`. This is a self-contained executable; Python and a separate runtime folder are not required. The local Windows build was tested under x64 emulation in a Windows 11 ARM64 VM.

Windows is unsigned; macOS uses a local development signature and is not notarised. Finish active sessions and close an earlier version before opening the same workspace in this release. Platform coverage and remaining feature limits are documented below.

Existing devices keep their saved credentials when upgraded. An encrypted copy of an older vault is saved before its first format upgrade. Upgraded vaults require 0.2.0 or later; see **First use** for details and rollback instructions.

## Run

Use Python 3.12 or newer. The declared minimum is Python 3.11; execution is currently verified with Python 3.14.4 on macOS and 3.14.8 on Windows. Python 3.11 has not been verified. Current Qt wheels determine the minimum supported operating system version.

macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pint_terminal
```

After setup, double-click `Launch PiNT Terminal.command` or run the module command above.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pint_terminal
```

After setup, double-click `Launch PiNT Terminal.bat`.

For a disposable visual preview with example hosts and no network connections:

```sh
python -m pint_terminal --demo
```

## Included

- A searchable vault of groups and devices, with SSH, Telnet, FTP and SFTP profiles and tags. Each group can store a standard username, password and SSH key path; individual devices can use their own credentials.
- Multiple terminal tabs with ANSI colour, cursor movement, alternate screens, keyboard input, selection, copy and paste.
- Disk-backed terminal scrollback with no fixed historical row limit. Only the active screen and visible historical rows are kept by the renderer. Available disk space is the practical limit.
- Encrypted output recordings which remain readable after disconnecting or restarting the application. Raw transcripts can be exported explicitly.
- SSH keepalives, cancellable reconnect backoff and manual reconnect. Clean SSH logout stops automatic reconnect. Input is never queued for replay across connections.
- SSH password, unencrypted private-key file and SSH-agent authentication. Unknown keys require fingerprint approval; changed keys are blocked.
- Dual-pane local/remote browsing, single-file uploads and downloads, new remote folders, progress and cancellation. Downloads use a temporary file and replace the destination only after success.
- A manually started, read-only TFTP host serving a selected folder. Defaults to localhost on UDP 1069. Files outside the root and symbolic links are rejected.
- Portable encrypted workspace export/import, including profiles, trusted host keys, window settings and recordings.

## First use

1. Create an encrypted workspace and choose a passphrase of at least 12 characters.
2. In **Vault**, choose **New group** for a site or project and set its default credentials. Select the group and choose **Add device to group**, then enter the device's hostname/IP, protocol and port.
3. New devices select **Use group credentials**. To override the defaults, edit the device and choose **Custom credentials**. Select a device and connect. For the first SSH/SFTP connection, verify the fingerprint before approving its key. Telnet login remains interactive.
4. Revisit completed output under **Recordings**. It is paginated by received chunks; the readable view interprets backspaces, carriage returns and line-editing controls, and hides formatting/control strings. Export retains the original text stream and control sequences.
5. Use **Export workspace** to create a `.pintbundle`. Import it on another device using the same passphrase.

Use **Lock & close** to disconnect, save and close the application. Reopening requires the workspace passphrase. Window size and the selected section are restored. Saved profiles move between devices; live network connections and remote shell processes do not migrate.

Group credentials apply as a complete set, including blank fields. Custom credentials also form a complete set: a blank custom password or key never falls back to the group. Changing a group updates the defaults used by new sessions and file operations. Existing terminal tabs keep their connection details through reconnects; open a new tab to use updated credentials. Move a device by changing its group in **Edit device**. Groups can be renamed, and empty groups can be deleted.

Older workspaces open with their existing device credentials preserved as **Custom credentials**. Select **Use group credentials** for devices that should share the defaults. On first save, the app keeps an encrypted copy of the original vault before upgrading its format. Upgraded workspaces and their exports require version 0.2.0 or a later compatible release; published 0.1.1 cannot open them. Recordings retain their existing format.

The original vault copy is saved beside the workspace as `name.v1-backup.pint`, with a numbered suffix if that name already exists. To return to an older app, close PiNT and preserve the upgraded files separately, then restore that copy under the original workspace filename beside its unchanged `.sessions.sqlite` file. This restores the pre-upgrade device settings; subsequent group and device edits are only in the upgraded vault.

## Data and security model

A workspace has two adjacent files:

- `name.pint`: AES-256-GCM encrypted groups, device profiles, passwords, trusted host keys and settings. Its key is derived with scrypt using a random salt and fixed bounded parameters.
- `name.sessions.sqlite`: encrypted session metadata, received text, events and rendered historical rows. SQLite exposes opaque identifiers and row ordering, not plaintext host names or output.

Workspace saves use atomic replacement. Recording writes use SQLite WAL and full synchronous commits, batched per received rendering chunk. Raw output is persisted in the transport thread before GUI rendering. Recording failures stop the session.

There is no cloud service or passphrase recovery. Back up exported bundles and keep the passphrase separately. Bundle imports are currently limited to 8 GiB of recordings and 16 MiB of workspace data; live history has no corresponding limit. Large archives are exported/imported synchronously.

Private key files are referenced by path and are not embedded in exports. Re-select them on another device or use its SSH agent. Trusted keys are encrypted in the workspace and materialised in a private temporary file while the app is open.

Typed input is not recorded separately. Any text echoed by the remote device, including a password or secret which it prints, is part of the recording. Raw transcript exports are deliberately unencrypted. Telnet, FTP and TFTP do not encrypt network traffic.

## Current limits

- Source execution and loopback protocol tests pass on macOS Apple Silicon and Windows 11 ARM64 using x64 Python under emulation. Native Windows ARM64, Intel macOS and real network-device compatibility remain unverified. See `docs/PLATFORM_ACCEPTANCE.md` for packaging and acceptance evidence.
- The terminal is based on pyte with additional alternate-screen and history handling. It is not a full xterm implementation. Mouse reporting, complex IME behaviour, terminal graphics and full Unicode/reflow compatibility are not claimed.
- The recording reader is a paged CLI transcript, not a full-screen or timed replay. Page boundaries may repeat the unfinished line as context. Backward navigation reconstructs prior control state and can be slower on very large recordings. Preview lines are bounded to 16,384 display columns and 64 code points per cell; omitted text is marked explicitly, while raw output remains intact. Searching across an entire archive, retention controls and recording deletion are not implemented.
- Reconnect creates a fresh remote session. It cannot restore a lost shell process; use a remote multiplexer when that is required. Telnet cannot reliably distinguish logout from a dropped TCP connection, so automatic reconnect is a per-profile choice.
- SSH MFA prompts, encrypted private-key passphrase prompts, a shared identity/keychain UI, jump hosts, forwarding, snippets, split panes and serial connections are pending.
- File transfers currently handle one file at a time. Remote rename/delete UI, recursive transfers, resumable transfers, FTPS and a transfer queue are pending. SFTP operations have a 120-second cap; FTP sockets have a 30-second timeout. Cancelled uploads may leave an incomplete remote file.
- TFTP currently supports octet-mode downloads with 512-byte blocks, retries and concurrent clients. It has no upload mode or negotiated block-size support. Serving a LAN requires selecting its interface address and allowing appropriate UDP traffic. The app does not request elevation for UDP 69.
- Automatic vault locking, passphrase rotation, multi-instance editing protection, signed installers and update delivery are pending.

## Verify and package

```sh
python -m pytest -q
python -m pint_terminal --demo --screenshot artifacts/hosts.png
python -m pip install -e '.[build]'
python -m PyInstaller --noconfirm --clean --distpath dist/0.2.0 "PiNT Terminal.spec"
```

On Windows, use `"PiNT Terminal Windows.spec"` in the packaging command to build the self-contained EXE. The original specification produces a macOS app bundle or a Windows folder build. The manual build workflow produces validation archives; release packaging uses the verified platform runtimes and their matching notices.

Tests use temporary workspaces and local loopback servers, with no production-device access. Qt tests run offscreen. Build on each target operating system; PyInstaller is not a cross-compiler. Build from the saved specification to retain the app identifier and version metadata. The macOS development bundle is not a notarised distribution release. [GitHub verification passed on macOS and Windows with Python 3.12](https://github.com/ReeceRRG12/PiNT-Terminal/actions/runs/40095170700) for the initial beta source.

`requirements-verified-macos.txt` and `requirements-verified-windows.txt` record the installed test environments, including optional packages; they are evidence, not cross-platform lockfiles. `pyproject.toml` defines the application dependencies. The runtime uses Qt Essentials and does not require a browser engine.

Planned work and platform verification are recorded in [the backlog](docs/BACKLOG.md) and [platform acceptance](docs/PLATFORM_ACCEPTANCE.md).

## Implementation references

The native shell uses [Qt for Python](https://doc.qt.io/qtforpython-6/), networking uses [AsyncSSH](https://asyncssh.readthedocs.io/en/latest/) and [telnetlib3](https://telnetlib3.readthedocs.io/), and terminal parsing builds on [pyte](https://pyte.readthedocs.io/en/latest/api.html). These components retain their respective licences. PiNT styling was checked against the existing PiNT Desktop theme.

[Third-party runtime notices](third_party/README.md) are included in the source and packaged downloads. They do not assign a licence to PiNT Terminal's own code.
