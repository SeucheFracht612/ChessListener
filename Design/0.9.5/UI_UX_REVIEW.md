# ChessListener 0.9.5 UI/UX review

## Executive conclusion

ChessListener's live board and engine-source colors are already recognizable. The weak point is the shell around them: navigation is cryptic, local workspaces are compressed into a live-overlay width, several states repeat information, and a few persistence/concurrency paths can silently attach data to the wrong object.

The proposed direction is **The Analyst's Desk**: a calm, compact chess instrument with a matte charcoal shell, warm board, score-sheet rules, monospaced notation, and restrained Stockfish-blue, Maia-amber, and played-move ochre. Live Analysis stays narrow and fast. Review and Studies become proper adaptive workspaces instead of taller versions of the overlay.

No production UI should be reskinned until the two data-integrity issues below are fixed.

## What was reviewed

- Firefox popup: idle, active, dismissed, switching, busy, and error behavior.
- Startup: Maia available, off, unavailable, short and constrained layouts.
- Global title bar: page identity, source state, actions, compact mode, and keyboard access.
- Live Analysis: waiting, searching, final, engine failure, syncing, preview, game end, and compact mode.
- Analysis Lab: loading, branches, live-game changes, save flow, and return-to-live behavior.
- Recovery: normal re-read, engine restart, metadata repair, exact FEN, validation, and stop session.
- Settings: every exposed setting, naming, grouping, defaults, reset, responsiveness, and missing high-value settings.
- Local Review: empty, ready, running, cancellation, failure, complete, historical exploration, graph, filters, and details.
- Saved Studies: empty, populated, search, editing, analysis, failure, branches, export, and deletion.
- Cross-cutting: layout stability, readability, focus, accessible naming, status truthfulness, error placement, and destructive actions.

The real PyQt screenshot harness renders 27 scenarios at 420×820 and 320×620: 54 deterministic screenshots plus geometry/text audits. The interactive concept covers ten screens, including the new Game Finished flow.

## P0: integrity before appearance

### 1. Review results can land on the wrong game

A running review can be cancelled and the selected/imported/deleted record can change before the worker's late completion is read. The queue result has no immutable game fingerprint or job generation, so it can populate or persist against the new record.

Required fix:

- Give each review job a generation and immutable game/settings fingerprint.
- Ignore progress and completion from any non-current token.
- Change Cancel to `Cancelling…` immediately.
- Guard import, delete, and record switching while a conflicting job is winding down.
- Invalidate the active job before deleting its game.

### 2. Study text edits can be silently lost

Title, branch name, and note edits live in widgets until `Save note`. Switching studies, searching, closing, or deleting can persist the stale model instead of the visible fields.

Required fix:

- Debounced local autosave with `Saving…` / `Saved locally`.
- Flush pending edits before every navigation or close.
- Retain dirty values and show a persistent inline failure if storage fails.
- Never rely on placeholders as the only field labels.

## Visual diagnosis

### What works

- The warm board is the strongest visual anchor and should remain.
- Stockfish blue, Maia amber, and played-move gold are already learnable and useful.
- Live Analysis is appropriately compact at normal width.
- Candidate rows, legal-move keyboard behavior, and state-source honesty are good foundations.
- Compact mode has the right information budget.

### What does not

- `–`, `↻`, `★`, `◆`, `≡`, and `×` form a tiny, cryptic navigation language.
- Review and Studies continue showing stale live source/turn state in the global title bar.
- Settings has a horizontal scrollbar even at the normal viewport.
- At 320 px, Review, Studies, Settings, and Recovery clip labels/actions or overlap content.
- Waiting → first result changes the board/candidate allocation and makes the layout jump.
- Preview can say `Exact` although the board is hypothetical and the evaluation belongs to the root.
- Review's graph lacks clear White/Black regions, current score, keyboard movement, and useful hover/focus detail.
- Explanations repeat the selected move, score, PV, ranking, and “no simple feature” more than necessary.
- Default Qt scrollbars, lists, progress bars, menus, and disabled states leak through the otherwise custom look.

## Visual identity: The Analyst's Desk

### Preserve

- Warm parchment/walnut board.
- Matte dark floating window.
- Stockfish blue square marker.
- Maia amber diamond marker.
- Played-move ochre bar/highlight.
- Exact/Inferred/Manual/Syncing truthfulness.

### Change

- Use thin score-sheet-like rules before adding more filled cards.
- Use system sans for interface copy and tabular monospace for moves, FEN, score, depth, and clocks.
- Give title actions coherent line icons, visible focus, accessible names, tooltips, and at least 32 px targets.
- Use 40 px primary actions.
- Separate stable page/mode identity from transient toasts and errors.
- Use one restrained danger treatment for Stop/Delete; do not add confirmation to reversible recovery/restart actions.
- Style hover, pressed, focus, selected, disabled, loading, error, and destructive states for every control family.

### Piece styles

Expose one Board setting:

- `Outline set`: both colors use outline glyphs in their respective parchment/ink colors.
- `Solid silhouettes`: both colors use filled glyphs in their respective colors.

Never mix outline white pieces with filled black pieces. The setting applies to Live, Lab, Review, and Studies together.

### Arrows

Use the same geometry for Stockfish and Maia: inset the shaft from the source, stop the head before the destination-piece center, add a quiet dark contrast halo, and separate arrows slightly when both leave the same square. Keep source color plus marker shape so color is never the only distinction.

## Screen recommendations

### Global title and navigation

- Make the title page-aware: Live, Preview, Lab, Review, Study, Recovery, Settings.
- Show position-trust badges only when they describe the board actually displayed.
- Hide the turn indicator on pages without an authoritative board.
- Keep the most relevant action visible; move secondary destinations into a labeled overflow menu at narrow widths.
- Scope Space/double-click compact mode to Live Analysis only.
- In standalone/local mode, label an app-closing action `Close`, not `Done` or `Return to live`.

### Startup

- Stack label/help above its field at narrow width.
- Keep only Live strength and Maia as first-run choices.
- Explain Maia as optional and independent of Stockfish.
- Avoid clipping the Maia description below the panel.

### Live Analysis

- Keep the board dominant and keep a stable reserved candidate region while searching.
- Show `Searching · depth N` / `Completed · depth N` without placing a spinner over the board.
- Candidate row: move, evaluation, depth, short continuation.
- Selected explanation: one `Why` statement and one expected reply/continuation, not a repeated transcript.
- Rename visible `PV` navigation to `Line`.
- Use `Last move` if `Played` remains ambiguous.
- Do not delay the first Stockfish frame for any visual behavior.

### Game finished

This should become the primary path into the local library and review features.

- Keep the final board visible.
- Show result and concise completion state.
- Primary: `Save game`.
- Secondary: `Run local review`, `Explore final position`, `Export PGN`.
- Offer `Automatically save completed games` in this panel and Settings.
- Keep automatic saving separate from automatic review.
- Make saving idempotent by game/session fingerprint so repeated end events cannot create duplicates.
- If history is incomplete, state that clearly and save the best honest record available rather than pretending a full PGN exists.

### Compact

- Keep evaluation, last move, best Stockfish line, Maia line, and Expand.
- Persist compact preference only if enabled by the user.
- Do not allow compact mode to resize Settings/Review/Studies.

### Preview and Analysis Lab

- Preview badge: `Preview · hypothetical line`; clarify that the root evaluation is still shown.
- Lab badge/ribbon: `Private branch · real game unchanged`.
- Keep Go Live persistent.
- When live changes, show `Live +N` without stealing focus.
- If Redo has sibling children, open a small continuation chooser.
- Save in place with `Saved locally`; provide `Open Study` as a secondary action instead of navigating automatically.

### Recovery

- Primary: `Re-read visible board`.
- Keep Restart Engines separate and explain that the position stays unchanged.
- Collapse metadata/FEN under `Advanced manual repair`.
- Spell castling rights as White/Black O-O and O-O-O.
- Label FEN persistently; validate inline, mark/focus the field, and retain values after host rejection.
- Give Stop Session the restrained danger style without a paternalistic confirmation.

### Settings

Prominent:

- Live strength
- Candidate lines
- Maia off/rating
- When the game advances
- Move explanation detail
- Lab strength
- Continuation length
- Review strength and alternatives
- Automatic save and automatic review
- Automatic study analysis and saved snapshots
- Evaluation perspective
- Piece style
- Board markings
- Opacity and always-on-top

Advanced but available:

- Stockfish threads
- Expanded candidate lines
- Classification sensitivity
- Coordinate visibility
- Reduced motion
- Remember compact mode
- Local Review/Studies board orientation override

Rename technical labels:

- `PV shown` → `Continuation length`
- `When live moves` → `When the game advances`
- `Expand` → `Expanded candidate lines`
- `Natural model` → `Maia human-move model`
- `Arrows` → `Board markings`
- `Analyse selected` → `Analyze positions when selected`

Reset should show `Defaults restored · Undo`, not a blocking confirmation.

### Local Review

- Empty state: Import PGN/FEN or select a saved game; hide dead board/graph/0% progress.
- Show progress only while reviewing; give cancellation an immediate state.
- Use a wider adaptive workspace for completed reviews.
- Graph: tinted White/Black halves, zero line, current value, move/classification tooltip, and keyboard Left/Right/Home/End.
- Timeline rows: move, classification text, evaluation swing/loss; no fixed-space pseudo-table.
- Summary: result, reviewed moves, both players' accuracy/loss, and turning points.
- Detail: Played, Best, Why, Continuation. Show the conservative explanation disclaimer once.
- Job generation/fingerprint checks are mandatory before visual refinement.

### Saved Studies

- Purposeful empty state; hide stale blank board/tree/editor.
- Adaptive two-column workspace when wide, stacked layout when narrow.
- Replace the crowded search/action row with a library list/drawer.
- Keep saved analysis visible while an updated analysis runs.
- Snapshot format: `+0.31 · d18`, with POV/bound/time in accessible detail.
- Add delete-subtree and choose-main-continuation actions.
- Autosave text fields and expose durable Saved/Saving/Failed state.

### Firefox popup

- Contextual primary text: Start analysis, Refresh this board, Reopen overlay, or Switch to this tab.
- Translate native/internal reason identifiers to actionable copy.
- Show Connecting/Refreshing/Stopping and `aria-busy` while actions run.
- Move protocol and snapshot IDs out of the normal status; surface protocol only for incompatibility.
- Preserve its existing aria-live/error/focus baseline and improve footer contrast/size.

## Responsive model

- Live overlay: maintain the compact narrow form; support 320 px through reflow, or declare an honest 340–360 px minimum.
- Review/Studies: remember separate workspace geometry and expand into a two-column layout when space exists.
- Below about 380 px: labels above controls, wrapped page actions, single-column post-game actions.
- Never use a page-level horizontal scrollbar.
- Elide long game/study titles but retain their full accessible value and tooltip.
- Keep board/candidate height stable across waiting/searching/results.

## Accessibility acceptance

- Visible focus on every keyboard action.
- Label buddies and accessible names/descriptions for every form field and icon.
- Eval bar announces POV, score or mate, bound, depth, and search/final state.
- Board focus announces piece/square, selected source, and legal target.
- Review graph has keyboard parity and announces selected ply, SAN, class, and evaluation.
- Minimum target: 32 px compact title actions and 40 px primary actions.
- Text/shape accompanies every source/classification color.
- Large font and 200% scale variants remain unclipped.

## Test plan and release gates

The PyQt visual harness should remain an audit test until the redesign lands. Then turn strict mode on in CI with tolerant perceptual baselines plus structural assertions.

Required sizes:

- 320×620
- 360×720
- 420×820
- wider Review/Studies workspace
- 100% and 200% scale
- default and larger system font

Required invariants:

- No clipped title, action, label, value, coordinate, or post-game action.
- Exactly eight board ranks and files; coordinate axes never participate in the square grid.
- No horizontal page scrollbar.
- No stale live badge on Review/Studies/Preview.
- No unexplained empty panel or idle 0% bar.
- No board-size jump on the first engine result.
- Piece mode is consistent across both colors and every board.
- Arrows stay within board bounds and do not cover the destination-piece center.
- Review completion is scoped to the correct immutable game.
- Study edits survive switch, search, close, and save failure.
- Auto-save is deduplicated and independent from automatic review.

## Recommended implementation order

1. Fix review-job identity and study edit persistence.
2. Introduce page-aware title state and responsive geometry.
3. Apply the identity tokens, icons, control states, and piece/arrow system.
4. Stabilize Live/Preview/Lab hierarchy without touching engine latency.
5. Add Game Finished save/review/explore flow and auto-save setting.
6. Redesign Recovery and Settings with progressive disclosure.
7. Expand Review and Studies into adaptive local workspaces.
8. Contextualize the Firefox popup.
9. Enable strict visual baselines only after all supported states pass.

## Deliberately unchanged

- No spectator-only or anti-cheat lockout.
- Bot/test games remain supported.
- Maia remains optional.
- Manual FEN recovery remains available.
- No telemetry, account, or cloud dependency.
- No sidebar-heavy dashboard, neon HUD, glow, glass, or card around every row.
- No unnecessary confirmation dialogs for scans, restarts, navigation, or reversible settings.
