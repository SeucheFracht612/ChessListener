# Changelog

## 0.2.1 — stabilization release

- Restored clean source builds and removed reliance on stale build artifacts.
- Completed last-move transport and corrected exact Played-move SAN conversion
  for ordinary one-ply tracking. Multi-ply catch-up may still use a fallback
  because its pre-final position is not yet retained.
- Added explicit version/capability handshakes between extension, native host,
  and overlay; incompatible protocol versions now fail closed.
- Made native and extension tests deterministic and added clean-build CI.
- Added validated, idempotent user installation plus diagnostics, update, and
  guarded uninstall paths.
- Preserved a fully validated installed Maia runtime when reinstalling from a
  source archive without optional engine payloads.
- Repaired engine submodule metadata and documented required Stockfish versus
  optional Maia dependencies.
- Removed the unproven prebuilt lc0 binary; `Native/Engine/lc0` is now an
  ignored, optional user-provided runtime validated during installation.
- Licensed ChessListener's original source under `GPL-3.0-only`, matching the
  open-source PyQt6 runtime used by the overlay.

The Firefox extension is still loaded temporarily and is not yet distributed
as a Mozilla-signed XPI.
