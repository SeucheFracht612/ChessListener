# ChessListener 0.9.5 visual identity proposal

Working name: **The Analyst's Desk**

## Design thesis

ChessListener should feel like a compact analysis instrument beside the board: calm, precise, slightly tactile, and unmistakably about chess. It should not look like a generic dashboard, an esports HUD, or a stack of modern rounded cards.

The proposal keeps the current warm board, dark floating window, Stockfish blue, Maia orange, and Played yellow. It gives those ingredients a stronger system:

- Matte charcoal surfaces feel closer to a tournament desk than pure black.
- Warm parchment and walnut anchor the interface in the physical chessboard.
- Blue is reserved for engine truth and active selection.
- Amber is reserved for human-likeness and Maia.
- Ochre is reserved for the played move and historical position.
- Thin ruled lines, numbered candidate rows, and monospaced notation borrow from score sheets without becoming decorative.
- Corners are lightly eased rather than pill-shaped. Panels are separated by rhythm and rules before boxes.

The identity should be recognizable from three elements alone: the four-square `CL` mark, the warm board palette, and the blue/amber/ochre analysis markers.

The chess set is a user preference rather than part of the state language. `Outline set` uses the outline glyph family for both colors, colored parchment and ink respectively. `Solid silhouettes` uses the filled family for both colors. The UI must never mix an outline white set with a filled black set.

## Core tokens

| Role | Token | Proposed value | Use |
| --- | --- | --- | --- |
| Window | `ink-950` | `#151714` | Outer background |
| Raised surface | `ink-900` | `#1c1f1c` | Title bar and primary sections |
| Control surface | `ink-850` | `#242824` | Inputs, selected regions |
| Divider | `line` | `#3a4039` | Hairlines and control outlines |
| Primary text | `paper-100` | `#f0ede5` | Labels and main values |
| Secondary text | `paper-400` | `#aaaFA7` | Explanations and metadata |
| Quiet text | `paper-550` | `#7d857c` | Depth, timestamps, tertiary copy |
| Board light | `board-light` | `#e8d5b4` | Light squares |
| Board dark | `board-dark` | `#987652` | Dark squares |
| Stockfish | `sf-blue` | `#5b8fc9` | Best line, analysis progress, active selection |
| Maia | `maia-amber` | `#d4914f` | Human-move prediction |
| Played | `played-ochre` | `#d4b84f` | Last/played move and review cursor |
| Success | `exact-green` | `#7db58f` | Exact source and healthy state |
| Warning | `warning` | `#d8a35b` | Syncing and recoverable issues |
| Error | `danger` | `#d36b70` | Destructive actions and invalid input |

The UI uses a 4 px base spacing rhythm, 1 px structural dividers, 4–6 px control radii, 8 px panel radii, and a 40 px minimum height for primary actions. Body text stays at 13–14 px; notation and scores use a monospaced face with tabular figures.

## Type and information hierarchy

- UI text: a neutral system sans (`Inter`, `Noto Sans`, `DejaVu Sans`, sans-serif).
- Moves, FEN, evaluations, depth, and PVs: a system monospace.
- Weight is used sparingly: regular body, medium headings and actions. Hierarchy comes mostly from spacing, tone, and alignment.
- Screen names use normal title case. Tiny uppercase labels are limited to short section markers such as `LIVE`, `LINE`, or `POSITION`.
- Technical metadata is present on demand, not allowed to compete with the move and evaluation.

## Signature components

### Four-square mark

A tiny 2 × 2 mark, with a `C` and `L` on opposite squares. It reads as a chessboard without needing a knight silhouette and survives at extension-popup size.

### Source tag

`Exact`, `Inferred`, `Manual`, `Syncing`, and `Lab` use the same compact rectangular tag. They always include text; color is reinforcement, never the only signal.

### Analysis marker

Each engine/source has a persistent geometric marker:

- Stockfish: blue square.
- Maia: amber diamond.
- Played move: ochre horizontal bar.

The marker appears on candidate lines, board arrows, review details, and settings. This makes the three concepts learnable without a legend on every screen.

### Candidate line

Candidate lines are structured rows, not independent cards. The move and evaluation are the primary scan path; depth is quiet and right-aligned. Only the selected line expands its PV and explanation by default. Selection uses a slim blue edge plus a low-contrast surface change.

### Score strip

The evaluation bar remains horizontal because the overlay is narrow. It becomes a thin, ruler-like strip with a fixed center tick and restrained labels. Search-in-progress keeps the prior position faintly and replaces the score with `Analyzing…`, avoiding a false blank state.

### Board arrows

Stockfish and Maia arrows share one geometric system: a short inset at the origin, a head that stops before the destination-piece center, a quiet dark halo for contrast, and a slight lateral offset when both arrows leave the same square. Color and the square/diamond line markers continue to distinguish their sources.

### Ruled note

Explanations and annotations use one thin accent rule on the left instead of another filled card. This visually connects engine reasoning, review explanations, and study comments.

## Screen-level direction

### Live analysis

The board remains dominant. Below it, the scan order is evaluation → played move → candidates → selected continuation → action. The current turn and source move to the title bar. Compact mode keeps the evaluation, played move, the best Stockfish line, the Maia line, and an explicit `Expand` action.

### Analysis Lab

Lab must look related but unmistakably non-authoritative. The source tag becomes `Lab`, and a narrow contextual ribbon states that the position is a private branch. `Go live` is persistent. Branch navigation and save controls sit together; engine candidates remain in the same place as live analysis to preserve muscle memory.

### Local Review

Review changes from a tall stack of equally weighted controls into a reading flow: game selector → board → evaluation timeline → current move explanation → move list. Import/export/delete live beside the selector as secondary library actions. The review action is primary only before analysis exists.

### Game finished

The final live position gains a small action panel rather than immediately throwing the user into another workspace. It states the result and offers `Save game`, `Run local review`, `Explore final position`, and `Export PGN`. A nearby `Automatically save completed games` preference is mirrored in Settings. Saving must be idempotent for the same game/session so repeated end signals cannot create duplicates. Automatic saving and automatic review are separate choices.

### Saved Studies

The current study is selected first. Board and branch navigation stay together. The variation tree reads like notation, with the evaluation aligned at the far edge. Title, variation name, and comment form one annotation area with explicit save feedback. Destructive study deletion is visually isolated from normal export and navigation.

### Settings

Settings use progressive disclosure. The few decisions that change everyday use are visible first: live strength, candidates, Maia, follow behavior, and explanation detail. Review, studies, display, and advanced engine controls are collapsible groups. Every setting states its unit or effect; no standalone technical noun such as `Expand` or `POV` is used without context.

### Recovery

The normal path is deliberately simple and dominant: explain the mismatch, then `Re-read board`. Engine restart is a separate maintenance action. Manual board-state reconstruction and exact FEN are under `Advanced manual repair`; the UI explains castling/en-passant implications and validates before applying. `Stop session` is isolated at the bottom.

### Startup and extension popup

Both reuse the same mark, source/status language, button hierarchy, and surfaces. Startup asks for the two meaningful first-run choices and makes everything else clearly adjustable later. The popup presents one current session and one primary action; protocol/version information is quiet and not part of the normal status sentence.

## Motion and state

- Evaluation changes glide for roughly 160–220 ms; initial rendering does not animate.
- New live positions cross-fade content very briefly but do not move controls.
- Search progress uses text plus a stable prior bar, not an indefinite spinner alone.
- A live update arriving in Lab produces one persistent amber `Live +N` control; it never steals focus.
- All motion respects reduced-motion preferences.

## Accessibility baseline

- Minimum contrast target: 4.5:1 for body text and 3:1 for large text and meaningful UI boundaries.
- Minimum pointer target: 32 px for compact title controls, 40 px for primary actions.
- Focus uses a 2 px light-blue ring with a 2 px offset; it is never removed.
- Every icon-only control has an accessible name and tooltip.
- Engine/source colors are paired with markers and text.
- Keyboard focus order follows the visual reading order on every screen.
- Long PVs, notes, file names, FENs, and imported game titles wrap or elide with a full accessible value.

## What this deliberately avoids

- Neon colors, glowing edges, glass blur, or gamer-console decoration.
- A card around every row or setting.
- Excessive pills and badges.
- Hiding chess information to achieve visual minimalism.
- Treating Recovery as a warning wall or blocking bot/test games.
- Rearranging core live-analysis controls between states without a strong reason.

## Prototype coverage

`ui-concept.html` contains interactive representations of:

- Live analysis, expanded and compact.
- Analysis Lab.
- Local Review.
- Saved Studies.
- Settings.
- Recovery.
- Startup.
- Firefox popup.

The prototype is a visual and interaction study only. It intentionally does not call native engines, read a browser board, or modify production settings.
