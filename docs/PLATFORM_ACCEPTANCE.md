# Platform acceptance

## 0.2.0 group/device vault verification

On 25 October 2026, the 0.2.0 source passed **133 tests and 7 subtests on each platform**, with no failures or skips: macOS 26.5.1 Apple Silicon, Python 3.14.4 (27.32 seconds); Windows 11 ARM64 10.0.22621.4317 using x64 Python 3.14.8 under emulation (126.41 seconds). Evidence is retained in `artifacts/macos-0.2.0-tests.xml` and `artifacts/windows-0.2.0-beta.1-tests.xml`. Removing the Beta UI wording for stable publication subsequently passed 17 focused GUI tests; application behaviour was unchanged.

Native Windows form checks exercised group creation, inherited and custom credentials, device selection/search, saving/reopening, and the minimum vault size at 125% scaling. Mac and Windows screenshots were visually checked. The synthetic macOS → Windows → macOS round trip preserved rotated group defaults, custom blank credentials, empty groups, Unicode settings and recordings. A legacy vault migrated on Windows with an exact encrypted original backup, then reopened with its recording on macOS. No production device or user vault was used.

The stable Mac DMG contains the 0.2.0 app and an Applications shortcut. Image verification, read-only mounting, copying the app out, deep/strict signature verification and native launch passed. The app retains an ad hoc signature and is not notarised. The standalone Windows EXE embeds its runtime and notices, supplies 0.2.0 file/product version metadata, and passed native Vault and Recordings launches from a temporary folder containing only the EXE. Both package source manifests match the release source. The Windows VM was restored to paused with idle pause on.

The initial Windows runner exceeded its four-minute overall timeout after 84 passing tests. The final diagnostic rerun completed without a stuck test or code correction. The earlier folder-package extraction encountered a transient DLL lock during temporary cleanup; bounded retry and repeat extracted-package launch passed. Qt emitted a DPI-awareness warning during source form checks, but the scaled layout and controls were inspected successfully. The Windows build retains system-DLL resolution warnings under ARM emulation, as in 0.1.1; native launch is checked separately.

Stable publication is a release designation, not evidence of every planned feature or platform. Conventional x64 hardware, Intel macOS, native Windows ARM64, extended physical keyboard/clipboard and real-device acceptance remain pending, together with signing/notarisation.

Beta publication verification on 24 October 2026: [GitHub CI passed on macOS and Windows with Python 3.12](https://github.com/ReeceRRG12/PiNT-Terminal/actions/runs/40095170700) for commit `8a8e196`. The historical evidence below describes earlier local checks.

Initial 0.1.0 evidence on 24 October 2026: final local suites pass on both tested environments. macOS 26.5.1 Apple Silicon with Python 3.14.4: **70 passed, 7 subtests passed** in 11.92 seconds. Windows 11 ARM64 (10.0.22621.4317) with x64 Python 3.14.8 under emulation: **69 passed, 1 skipped, 7 subtests passed** in 36.00 seconds. The skip is a symlink fixture requiring unavailable Windows privileges. This does not establish native Windows ARM64 or conventional x64 hardware acceptance. GitHub workflows target Python 3.12 but have not been dispatched.

Protocol evidence: `tests/test_transport.py`, `tests/test_transfers.py` and `tests/test_tftp.py` pass on both platforms, subject to the Windows symlink skip. These cover stalled SFTP timeout/cancellation, preserved download destinations and FTP cancellation using loopback servers. Windows testing found and corrected archive flushing on a read-only handle and SFTP temporary-file cleanup. Qt event tests also cover Windows AltGr text and macOS Ctrl/Option behaviour.

macOS packaging evidence: the Apple Silicon development bundle launched through Launch Services, displayed the native demo window and closed after capturing its own interface. Identifier/version metadata and the local deep/strict code-signature check passed. This verifies basic native launch and rendering; extended physical keyboard, clipboard and real-device acceptance remain pending.

Synthetic portability evidence: macOS exported an encrypted bundle with a profile, password, Unicode settings and a recording. Windows imported it into a path containing spaces and `café`, validated the contents, added a recording and exported again. macOS imported the return bundle and verified both recordings and the saved credentials/settings. Private-key path relocation and large real archives still need manual checks.

Native Windows source launch rendered the Qt interface and captured its own window successfully. Evidence is retained locally in `artifacts/windows-native-source.png`, `artifacts/windows-tests.xml` and `artifacts/macos-tests.xml`. The release tag identifies the published source.

Final package evidence: `dist/PiNT Terminal.app` passed Launch Services launch/capture and deep/strict local signing checks after the scrollable details-panel correction. `dist/PiNT-Terminal-Windows-x64.zip` contains the clean Windows build, whose executable passed native Hosts and Terminal demo launches. The copied ZIP passed CRC verification (225 files, 48,816,879 bytes). Windows build logs contain unresolved system-DLL warnings under ARM emulation; conventional x64 hardware acceptance remains pending. Final previews are `artifacts/macos-packaged-final.png` and `artifacts/windows-packaged-page0.png` / `page1.png`.

The final layout-only change passed two existing Mac GUI tests and constrained 125% layout inspection after the full suites. The VM's temporary idle-pause change was restored and verified. These are development packages, not signed/notarised distribution releases.

## 0.1.1 maintenance verification

- macOS full suite: **94 passed and 7 subtests passed**, 12.72 seconds. Three recording GUI regressions also passed after the final raw-export newline correction.
- Windows full suite: **93 passed, 1 symlink-permission skip and 7 subtests passed**, 49.38 seconds. Same isolated Python/OS environments as the initial alpha.
- Added regressions cover reopened recordings, backspace-space prompt erasure, carriage-return/ANSI line editing, Unicode, fragmented controls across chunks/pages, bounded malformed controls, explicit preview truncation and exact UTF-8 raw exports.
- Real loopback `localhost` SSH and SFTP connections pass after key approval. Tests retain changed-key rejection, duplicate approval, numeric/CIDR matching and port-specific revoked-key rejection, including a fallback trusted key. No production hostname was contacted.
- Synthetic visual fixture: `artifacts/recording-fixed-0.1.1.png`. Test results: `artifacts/macos-0.1.1-tests.xml` and `artifacts/windows-0.1.1-tests.xml`. The first Windows run revealed newline translation in export; the final passing run includes its correction.
- 0.1.1 builds live under `dist/0.1.1`, leaving previous builds intact. Native packaged macOS Recordings and Windows Hosts/Recordings launches passed. Mac 0.1.1 metadata/signature and Windows ZIP CRC checks passed. The VM was returned to paused with idle pause on. Build logs and checksums are retained locally.

## Automated evidence to collect

- Run `verify.yml` on both platforms and retain test results and the demo screenshot.
- Check Python 3.11 separately before claiming support for the declared minimum version. The current workflow covers 3.12 only.
- Run `build.yml`, download each artefact onto its target OS, extract it and launch it. macOS archives must preserve executable permissions and application symlinks.
- Record the OS version, CPU architecture, Python/dependency versions and tested commit. A build on one macOS architecture does not establish compatibility with both Intel and Apple Silicon.

## Manual acceptance, pending

- Launch as an ordinary user, including a workspace path containing spaces and non-ASCII characters. Check native menus, clipboard shortcuts, keyboard input, window resizing and display scaling.
- Connect to representative switches/servers with SSH password, private-key and agent authentication. Verify unknown-key approval, changed-key blocking and no repeated authentication attempts after a rejected password.
- Use SSH and Telnet in multiple tabs. Check ANSI output, cursor movement, terminal resizing, paging and long output. Disconnect, restart the application and inspect the saved recording.
- Interrupt connectivity, restore it and confirm reconnect. Ensure typed input is not replayed after reconnection. Confirm intentional SSH logout stays disconnected and manual Disconnect cancels retry.
- Upload and download binary files through FTP and SFTP, compare hashes, cancel an active transfer and preserve an existing destination after failure. Test the actual FTP server's listing behaviour.
- Start TFTP only on the chosen interface/root, retrieve an octet file from a representative device, then stop it. Verify write requests and paths outside the chosen root fail. Devices fixed to UDP port 69 need an explicitly selected compatible port and host configuration; the app defaults to 1069.
- Transfer an encrypted workspace between macOS and Windows, unlock it and inspect saved profiles and recordings. Check external private-key paths on the destination device and approve host identity where required.
- Quit during a session, reconnect wait and transfer. Verify shutdown leaves no running worker, partial file presented as complete or unreadable recording.

Release evidence should distinguish passing loopback tests, offscreen rendering, interactive OS checks and real-device checks. The Early Access Beta still requires further feature and compatibility testing.

## Early Access Beta publication

The `v0.1.1-beta.1` release is explicitly marked as a GitHub pre-release. The app and README use the Early Access Beta label. Only that label and packaging/public documentation changed after the 0.1.1 functional verification above. Local machine recovery notes, saved vaults, recordings, screenshots and working environments are excluded from source control. Platform build archives are distributed as release assets.
