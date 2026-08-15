# Engine runtime

ChessListener uses two independent analysis paths:

- **Stockfish is required** and is installed as a system package. It provides
  objective evaluation and principal variations.
- **Maia is optional** and runs through lc0. It predicts a human-like move at a
  selected rating. Missing Maia files must not prevent Stockfish-only use.

## Expected layout

```text
Native/Engine/
├── lc0                              # local, ignored, user-provided executable
├── lc0-src/                         # source submodule
└── maia-chess/                      # Maia submodule
    └── maia_weights/
        ├── maia-1100.pb.gz
        ├── maia-1200.pb.gz
        ├── ...
        └── maia-1900.pb.gz
```

Initialize the source and model repositories with:

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

The repository does not distribute a prebuilt lc0 executable. Build lc0 from
the pinned `lc0-src` submodule (or obtain a build whose exact provenance and
license materials you trust), then copy the result into the ignored local
path:

```bash
install -m 0755 /absolute/path/to/your/lc0 Native/Engine/lc0
```

Follow lc0's maintained upstream build instructions rather than relying on a
potentially stale command here. The local executable must target Linux x86-64
and must have every library shown by `ldd`, commonly including an OpenBLAS
runtime:

```bash
ldd Native/Engine/lc0
```

No output line may end in `not found`.

## Validation

The supported validation path is:

```bash
./Native/install.sh --check
```

For a fresh install where Maia must be available:

```bash
./Native/install.sh --require-maia
```

The installer does more than check filenames. It requires all nine rating nets
(1100, 1200, …, 1900), validates every gzip stream, checks the user-provided
lc0's dynamic dependencies, and completes a bounded UCI handshake with **each**
rating net. Maia is copied to the stable runtime only after every check passes.
The local `Native/Engine/lc0` file stays untracked. Because each engine start
has its own 30-second ceiling, validation can take several minutes on a broken
or unusually slow lc0 build; failures identify the exact rating.

To inspect the handshake manually:

```bash
printf 'uci\nquit\n' | timeout 30 \
  Native/Engine/lc0 \
  --weights=Native/Engine/maia-chess/maia_weights/maia-1100.pb.gz
```

## Runtime overrides

The native analysis layer supports these developer overrides:

| Variable | Meaning |
|---|---|
| `CHESSLISTENER_STOCKFISH` | Absolute path to a Stockfish-compatible UCI engine. |
| `CHESSLISTENER_LC0` | Absolute path to an lc0-compatible UCI executable. |
| `CHESSLISTENER_MAIA_NET` | Absolute path to one Maia weight file, overriding the selected rating file. |
| `CHESSLISTENER_LC0_BACKEND` | lc0 backend name such as `eigen` or `blas`. |
| `CHESSLISTENER_OVERLAY` | Absolute path to an alternate overlay script, mainly for tests. |

Firefox-launched native hosts inherit the environment from the Firefox
process. Completely exit Firefox before launching it with an override.

These variables are development/diagnostic controls. The normal installation
uses paths relative to the stable installed host for lc0, Maia, and the
overlay; it does not depend on the current working directory.

## Updating or replacing lc0

A replacement must:

1. target Linux x86-64;
2. be executable and complete an ordinary UCI handshake;
3. accept an lc0-style `--weights=PATH` argument;
4. load the supplied Maia protobuf networks;
5. have no unresolved dynamic libraries.

Re-run `./Native/install.sh --require-maia` after changing the executable or
weights. Do not force-add the local binary. If a future release distributes
lc0, first record its exact source and reproducible build, then satisfy its
redistribution license.

## Licenses and redistribution

lc0, Maia, and the Maia weights are third-party works. A local lc0 executable
is not part of the repository. ChessListener's original source is licensed
under `GPL-3.0-only`, but that repository license does not relicense any engine,
submodule, executable, or model weight. Anyone publishing binaries or a
packaged release must provide notices, license texts, and corresponding
source/build information as required by each upstream license. Start with
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) and the license
files in both submodules.
