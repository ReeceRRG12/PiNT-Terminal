# PiNT Terminal backlog

The goal is a complete PiNT terminal and network tools application. This list distinguishes the Early Access Beta from the planned feature set and its acceptance checks.

## Next verified stage

1. Complete extended interactive Windows acceptance: physical keyboard layouts, native clipboard, scaling and extracted-package launch on a conventional x64 PC. Automated suites and synthetic Mac/Windows workspace round trips now pass in the Parallels VM.
2. Validate against representative SSH, Telnet, FTP and SFTP devices. Obtain explicit target details before connecting. Exercise a long command, network loss/recovery and intentional logout.
3. Run representative interactive programs and 24-hour sessions with sustained output. Measure memory/disk growth and terminal correctness. Set a device/application compatibility matrix before expanding the emulator.
4. Exercise TFTP on a chosen LAN interface with a real switch, including its expected UDP port and file size.

## Replacement features

- Search and filter complete recordings; stream clean text exports; retention/delete controls; timed terminal replay.
- Shared credential identities and keys; encrypted key passphrases; keyboard-interactive authentication; explicit host-key management.
- SSH jump hosts and tunnels; local/remote/dynamic port forwarding.
- Saved tab arrangements, split panes, snippets and serial connections.
- Multi-file/recursive transfers, queue/resume, remote rename/delete and FTPS.
- TFTP uploads, opt-in overwrite policy and negotiated options if needed by target devices.
- Optional vault idle lock, passphrase changes and safe concurrent workspace editing.
- Signed macOS/Windows packages, supported OS/CPU matrix, versioned migrations, update delivery and dependency notices.

## Deliberately outside this first build

Cloud synchronisation, collaboration and shared accounts. Portable local bundles meet the requested cross-device transfer workflow without operating a new service. Cloud features require a separate scope decision.
