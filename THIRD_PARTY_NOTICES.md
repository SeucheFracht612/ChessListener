# Third-party notices

ChessListener interoperates with or contains the third-party components below.
This summary is provided for attribution and release engineering; it is not a
substitute for the complete upstream license texts or legal advice.

## Components represented in this repository

### Leela Chess Zero (lc0)

- Upstream: <https://github.com/LeelaChessZero/lc0>
- Repository location: `Native/Engine/lc0-src` (Git submodule)
- Recorded source revision: `fd71a2d9` (lc0 v0.32.1)
- Upstream license: GNU General Public License, version 3 or later

The complete license and copyright notices are in the lc0 source submodule.
Release 0.2.1 removes the previously tracked prebuilt `Native/Engine/lc0`
because its exact provenance and reproducible build command were not recorded.
That path is now ignored and reserved for an optional user-provided local
executable. If a future distributor bundles lc0, it must establish the binary's
provenance and satisfy the GPL's corresponding-source and notice requirements;
a submodule URL alone should not be assumed to fulfill every obligation.

### Maia Chess

- Upstream: <https://github.com/CSSLab/maia-chess>
- Repository location: `Native/Engine/maia-chess` (Git submodule)
- Runtime artifacts: Maia rating network files under `maia_weights/`
- Upstream license: GNU General Public License, version 3

Consult the submodule's `LICENSE`, model documentation, citations, and
copyright notices before redistributing its code or trained network files.
Retain the upstream attribution and any notices that apply to the weights.

## Required external runtime components

These are expected to be installed by the user and are not vendored by this
repository.

### Stockfish

- Upstream: <https://github.com/official-stockfish/Stockfish>
- Purpose: objective UCI chess analysis
- Upstream license: GNU General Public License, version 3

ChessListener launches the user's system Stockfish executable. If a future
ChessListener package bundles Stockfish, that distributor must add the exact
binary's notices and corresponding source offer/materials.

### PyQt6 and Qt

- PyQt upstream: <https://www.riverbankcomputing.com/software/pyqt/>
- Qt upstream: <https://www.qt.io/>
- Purpose: desktop overlay user interface
- PyQt6 open-source license: GNU General Public License, version 3 only

PyQt6 is offered under GPL and commercial licensing terms; ChessListener uses
the GPL option and aligns its original source under `GPL-3.0-only`. Qt has its
own open-source and commercial licensing terms. ChessListener currently imports
a user-installed PyQt6 runtime rather than vendoring it. Packaging or
redistributing either framework requires reviewing the exact selected license,
version, linked Qt modules, and accompanying notice/source obligations.

### OpenBLAS and other shared libraries

- OpenBLAS upstream: <https://github.com/OpenMathLib/OpenBLAS>
- Purpose: optional lc0 numerical backend/runtime dependency

A user-provided lc0 executable may be dynamically linked against OpenBLAS plus
standard system libraries. `ldd Native/Engine/lc0` identifies its libraries on
a particular system. A packaged distribution must inventory the actual
libraries it ships rather than relying only on this development-time list.

## ChessListener's own license

Copyright (C) 2026 SeucheFracht612.

Except for the third-party components and materials identified above,
ChessListener's original source code is licensed under the GNU General Public
License, version 3 only (`GPL-3.0-only`). The complete terms are in
[`LICENSE`](LICENSE). That license does not relicense the engine submodules,
model weights, user-installed dependencies, or locally supplied executables.

Before publishing a release, the owner should at minimum:

1. record exact third-party versions/commits and binary provenance;
2. include complete required license and copyright texts;
3. provide corresponding source and build instructions where required;
4. verify whether the Maia model weights carry any additional terms or
   requested academic citation.
