# Changelog

## 0.3.0 — session and recovery release

- Added deterministic ownership of one active browser game so snapshots from
  multiple tabs can no longer corrupt one native chess state.
- Added a Firefox toolbar popup for inspecting the owner, explicitly switching
  to the current tab, re-reading its board, and stopping analysis.
- Added session lifecycle messages for navigation, game completion, tab close,
  and explicit stop; overlay dismissal is sticky for the current session.
- Added one bounded automatic native-host reconnect with latest-snapshot replay
  after an unexpected failure.
- Added a Recovery page in the existing overlay style with visible-board state
  replacement, exact FEN override, engine restart, re-scan, and session stop.
- Made board orientation an independent update instead of waiting for the next
  piece movement.
- Added page-instance, route-generation, session, and snapshot sequence guards
  so late asynchronous work cannot roll a game backwards or cross sessions.
- Advanced extension, host, and overlay negotiation to protocol 2 / version
  0.3.0 and expanded deterministic lifecycle/recovery coverage.
- Kept participant-versus-spectator policy outside the software: bot games and
  other local testing remain usable, while the documentation states that the
  user is responsible for following applicable rules.

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
