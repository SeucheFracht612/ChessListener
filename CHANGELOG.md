# Changelog

## 0.9.5 — The Analyst's Desk

- Reworked every native screen around a quieter **Analyst's Desk** visual
  system: matte charcoal surfaces, warm score-sheet rules, page-aware
  navigation, consistent engine markers, complete control states, visible
  keyboard focus, and responsive layouts from the compact live overlay to
  wide Review and Studies workspaces.
- Added a dedicated finished-game panel that keeps the final board visible and
  offers idempotent local saving, Local Review, final-position exploration,
  and PGN export. Completed games can be saved automatically without also
  enabling automatic review; an exact visible result is retained when the
  page exposes one, otherwise the record remains honestly unscored. If move
  history is unavailable, the validated final FEN is saved as an explicitly
  position-only record instead of borrowing another selected library game.
- Added one global board-piece preference: **Outline set** or **Solid
  silhouettes**. Both sides always use the same glyph family in their own
  light/dark colours across Live, Analysis Lab, Review, and Saved Studies.
- Refined Stockfish and Maia arrows with common inset geometry, target-aware
  heads, a contrast halo, lateral separation for shared origins, and their
  existing square/diamond source markers.
- Reorganized Settings into everyday choices plus collapsible Lab, Review,
  display, and advanced-engine sections. Added piece style, coordinates,
  reduced motion, always-on-top, remembered Compact mode, automatic completed
  game saving, and a reversible Reset defaults action.
- Simplified Recovery around **Re-read visible board**, moved exact-state
  repair under progressive disclosure, and retained explicit engine restart
  and session-stop actions without blocking bot or test games.
- Redesigned the Firefox popup with contextual Start, Refresh, Reopen, and
  Switch actions, explicit busy states, accessible error explanations, and
  quiet protocol details shown only when they are relevant. Firefox chrome now
  uses the same warm-board **CL** mark as the desktop visual system.
- Made review workers generation-, game-, and settings-scoped so late progress
  or completion can never attach to a different selected record. Cancelling,
  switching, importing, and deletion now have deterministic transition state.
- Made study titles, branch names, and notes local-first with debounced atomic
  autosave, visible Saving/Saved/Failed feedback, and a navigation/close guard
  that retains edits after a storage failure.
- Moved saved games, studies, and review caches out of the replaceable runtime
  into `$XDG_DATA_HOME/chess-listener-library/reviews.json`. Legacy libraries
  are preserved with an atomic create-without-overwrite migration; a different
  existing destination wins and the old bytes become a separately reported
  recovery copy. Install/update/uninstall dry runs are read-only, and uninstall
  now proves legacy data is safe before deleting its marked runtime. Existing
  malformed, unreadable, oversized, or concurrently changed archives fail
  closed and remain untouched while live analysis stays available.
- Made the optional Maia `Engine/lib` directory part of the validated managed
  payload: a source replacement clears stale backend libraries before copying
  its own, invalid/no-Maia cleanup removes them, and the validated-installed
  preservation branch remains byte-for-byte unchanged.
- Added a real offscreen-PyQt visual audit covering startup, Live, Compact,
  Analysis Lab, finished games, Recovery, Settings, Review, Studies, both
  piece styles, errors, busy states, narrow/normal/wide layouts, and enlarged
  text. Structural clipping/overlap checks accompany the rendered contact
  sheet for human visual review.
- Kept protocol 4: the optional verified game-result field is additive.
  Extension, host, and bundled UI are version 0.9.5.

## 0.9.0 — Saved Studies

- Added persistent local study trees. Any Analysis Lab branch can be saved,
  reopened after the live game or Firefox session ends, and continued by
  moving pieces on the same interactive board.
- Added named variations and per-position comments, plus a searchable study
  selector. Variation branches are shown as a collapsible tree with Root,
  parent, and first-continuation navigation.
- Added optional automatic local Stockfish analysis for selected study
  positions. Bounded MultiPV evaluation snapshots are stored with the node and
  retain score bounds, depth, and legally validated continuation moves.
- Added full annotated study PGN export with recursive side variations (RAV),
  names, comments, evaluation/depth tags, and SetUp/FEN headers for custom
  roots. Corrupt, cyclic, unreachable, mismatched-FEN, and illegal trees are
  rejected transactionally.
- Migrated the local JSON library from schema 1 to schema 2 without discarding
  existing games or cached reviews. The combined archive remains atomically
  replaced and capped at 16 MB; studies are limited to 100 trees and 512
  positions each.
- Added Saved Studies settings for automatic position analysis and evaluation
  snapshot persistence. Study analysis is local and isolated from live,
  Recovery, Maia, and native Analysis Lab state.
- Kept protocol 4 because studies are entirely local to the PyQt UI. Extension,
  host, and bundled UI are version 0.9.0.

## 0.8.0 — Local PGN/FEN Import

- Added dependency-free PGN import for one game at a time. Comments, NAGs,
  nested side variations, figurine notation, castling, promotion, and custom
  starting FENs are supported; only the main line is retained.
- Made imports fail closed: every SAN or coordinate token must match one legal
  move and the complete main line is converted to canonical UCI before it can
  enter Review Explorer or the library. Malformed, ambiguous, oversized, and
  multi-game files are rejected without replacing the current record.
- Added direct six-field FEN import, including zero-move position analysis and
  immediate local exploration.
- Added a standalone local-review launch mode (`overlay.py --local`) which does
  not require Firefox, a Chess.com page, native messaging, or network access.
- Preserved common PGN metadata in the bounded atomic library and annotated
  export. Imported records are saved before review; setting-specific engine
  caches now retain every position's analysis, including a single imported
  FEN.
- Kept protocol 4 because browser/native messages are unchanged. Extension,
  host, and bundled UI are version 0.8.0.

## 0.7.0 — Review Explorer

- Added a clickable White-POV evaluation graph tied to every reviewed ply,
  with keyboard and Back/Forward/Final navigation.
- Added review filters for turning points, all errors, major errors, and
  forcing moves, plus separate White/Black average-evaluation-loss and error
  summaries.
- Added local historical exploration after the browser game has ended. Any
  reviewed position can become an interactive legal branch with promotion
  choice, undo, best-move arrow, MultiPV continuations, and generation-scoped
  Stockfish results. It never changes live or Recovery state.
- Added a bounded local library (50 games / 16 MB) with atomic writes, deletion,
  multiple review-setting caches per game, and instant reuse of identical
  strength/line/sensitivity/thread reviews.
- Kept protocol 4 because browser/native messages are unchanged. Extension,
  host, and bundled UI are version 0.7.0.

## 0.6.0 — Local Game Review

- Added a fully local post-game review which re-analyses every verified game
  position with a separate Stockfish process. Review work cannot delay the
  live Stockfish/Maia lanes or change the authoritative game position.
- Added a navigable, colour-coded move timeline with Best, Excellent, Good,
  Inaccuracy, Mistake, and Blunder classifications; mover-relative evaluation
  loss; turning-point counts; best alternatives; depth; and SAN continuations.
- Added review settings for per-position strength, one to five alternatives,
  and optional automatic review when a completed game ends. Reviews expose
  progress and can be cancelled.
- Added annotated PGN export. The native rules engine transports the full
  legally verified game as canonical UCI, including custom starting FENs,
  castling, en-passant, promotions, and rewritten bot histories.
- Review labels are deliberately conservative. They describe the configured
  local search and are not Chess.com accuracy scores or causal explanations.
- Kept native protocol 4: the game-record frame is additive. Extension, host,
  and bundled UI are version 0.6.0.

## 0.5.0 — Analysis Lab

- Added a session-scoped Analysis Lab whose positions are isolated from the
  authoritative Chess.com game and Recovery state. The existing Stockfish and
  Maia workers analyse either the live board or the selected exploration node
  without starting a second set of engines.
- Made the miniature board interactive in Explore mode with legal drag,
  click, and keyboard moves, explicit promotion choice, branch navigation,
  and an instant return to the latest live position.
- Added selectable MultiPV candidate rows, SAN principal variations, read-only
  PV stepping, and the ability to begin an editable branch from a previewed
  continuation.
- Added deterministic **What the line shows** explanations for legally proven
  features such as checks, captures, recaptures, castling, promotion, mate,
  candidate-score differences, and the actual material at the displayed
  horizon. The UI does not invent strategic intent that Stockfish did not
  expose.
- Added settings for candidate count, displayed PV length, explanation detail,
  evaluation perspective, Explore strength, live-follow behaviour, expanded
  rows, arrow visibility, and disabling Maia.
- Kept live capture and reconciliation active while exploring. A live update
  is reported without overwriting the branch; returning to Live immediately
  analyses the newest authoritative position.
- Advanced extension, host, and overlay negotiation to protocol 4 / version
  0.5.0 with explicit live-versus-explore target identities and stale-result
  rejection.

## 0.4.1 — bot-game synchronisation hotfix

- Fixed incidental Chess.com board-class mutations canceling the only delayed
  recovery request and leaving the overlay permanently on Syncing.
- Added move-history adapters for Chess.com's paired bot rows, private-use
  icon glyphs, and piece classes, including the exact reported ten-ply game.
- Added a guarded inferred fallback after bounded recovery fails: a later
  board is accepted only when it is exactly one unique legal move from the
  preceding observation. Idle or ambiguous boards remain untouched.

## 0.4.0 — exact-state and fast-analysis release

- Shortened the normal board-capture path while retaining a confirming read,
  so an ordinary move reaches the native host without waiting for history or
  recovery work.
- Added delayed Chess.com move-history reconciliation. A history candidate is
  accepted only after complete legal replay produces the displayed board;
  invalid, incomplete, stale, or polluted move lists cannot replace state.
- Reconstructed side to move, castling, en-passant, move counters, and the
  final UCI move from validated UCI/SAN history, including bot-game takebacks.
- Moved multi-ply board inference behind a delayed recovery request with one
  shared node budget and a hard wall-clock deadline. A mismatch now retains
  the last trustworthy board and evaluation while synchronising.
- Added compact Exact, Inferred, Manual, and Syncing state indicators without
  changing the overlay's overall layout.
- Split Stockfish and Maia into independent latest-position workers. Slow
  Maia queries and rating reloads no longer delay Stockfish's first result;
  same-revision partial results are merged instead of erasing one another.
- Advanced extension, host, and overlay negotiation to protocol 3 / version
  0.4.0 and added deterministic history, recovery, supersession, and engine
  latency coverage.

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
