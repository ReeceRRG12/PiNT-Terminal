# Third-party notices

PiNT Terminal 0.2.0 includes the following runtime software.
The upstream copyright and licence texts are in `licenses/`.

- CPython: 3.14.4 on macOS and 3.14.8 on Windows.
- Qt, PySide6 Essentials and shiboken6: 6.12.0.
- AsyncSSH, telnetlib3, cryptography, cffi, pycparser, pyte, wcwidth and typing_extensions.
- Windows terminal support: blessed, jinxed and ansicon.
- The PyInstaller bootloader and runtime hooks, including their distribution exception.
- Native libraries included with the Python runtime, with separate notices where platform versions differ.

`components.json` records the component versions, applicable platforms, upstream
project or source URLs, and notice files. It is the component index for this release.
`qt-attributions.json` indexes the copyright and licence notices published for Qt
6.12.0 Core, GUI, D-Bus, Network and Image Formats. This module-level collection
also retains notices for platform-specific Qt code. `common-licences.json` indexes
the full licence texts referenced by those attributions.

The Python package notices were copied from the installed runtime distributions.
The Qt and PySide licence texts were obtained from their official source
repositories at tag `v6.12.0`. Qt attribution copyright and licence blocks were
retained from the official Qt 6.12.0 documentation, with their source links.
The corresponding full licence texts came from the official SPDX licence data.
The Windows native runtime versions are recorded in the CPython 3.14.8 source;
its OpenSSL and SQLite versions also match the packaged DLLs. The macOS native
library notices came from the installed runtime packages.

The upstream cryptography software bills of materials are retained in
`sbom/cryptography/`. They record the components used within its native extension.

Keep this directory with both platform distributions. Licence and attribution
texts retain their original spelling and formatting. This notice collection does
not change the licence terms of any component.
