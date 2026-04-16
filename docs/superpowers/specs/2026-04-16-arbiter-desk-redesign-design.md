# Arbiter Desk Redesign Design

Date: 2026-04-16

## Goal

Redesign the Arbiter public dashboard and operator dashboard into a premium, dense, high-volume trading product where UI quality remains stable under heavy data volume. The redesign must stay visually polished on desktop, tablet, and mobile while avoiding text truncation, hover-state distortion, layout drift, endless page growth, and sloppy control alignment.

This phase is UI-only. It does not change backend behavior, mapping logic, validation logic, or downstream execution flows.

## Outcome

The approved direction is a `blotter-first` trading desk rendered in a reference-inspired visual system:

- soft sage application backdrop outside the product shell
- monolithic black primary workspace with large radii and minimal chrome
- lime accent used sparingly for live focus, positive state, and selected controls
- oversized left-side primary chart as the visual anchor
- right-side risk and recent-trades rail for secondary context
- compact metrics strip below the chart
- strong spacing discipline, quiet typography, and dense-but-readable cards

This is not a generic crypto wallet clone. The composition follows the approved reference, but the content remains native to Arbiter: route activity, portfolio risk, opportunity scanning, execution state, mapping review, and operator workflows.

## Scope

### Included

- public dashboard shell and content alignment polish
- operator dashboard shell and control alignment polish
- hover, focus, pressed, and active-state stability for all interactive controls
- Activity Atlas compaction and readability improvements
- high-volume mapping experience redesign
- desktop, tablet, and mobile spacing, overflow, and truncation cleanup

### Excluded

- backend APIs or payload shape changes
- changes to event generation, mapping logic, or execution behavior
- changes to risk, strategy, or validator semantics
- new data dependencies that require downstream implementation work

## Information Architecture

### Desktop

Desktop is organized around one dominant story and one secondary rail:

1. Top navigation bar
   - brand mark
   - primary navigation: dashboard, markets, scanner, trade, portfolio, infrastructure
   - search, operator identity, alerts
2. Primary hero area on the left
   - live desk value headline
   - time-range controls
   - large activity chart with vertical market bars and lime performance line
3. Secondary rail on the right
   - portfolio risk score
   - recent trade cards
   - compact contextual summaries
4. Lower operational workbench
   - scanner opportunity surface
   - Activity Atlas surface
   - mapping workspace
   - operators, incidents, collectors, and infrastructure panels

### Trading Surface Mapping

Even when the visual hierarchy resembles the reference, the actual product behavior remains blotter-first:

- the large left chart represents live trade activity, portfolio value, or route performance
- the right rail represents recent trades, trade outcomes, or risk posture
- deeper operational surfaces such as scanner details, incident queues, mappings, and collectors remain bounded within internal panes instead of extending page height indefinitely
- any row-level action surface stays tied to the currently selected route or mapping instead of becoming a disconnected page

## Visual System

### Color

- outer background: muted sage-green field
- app shell: true black / near-black
- cards: dark graphite with low-contrast separation
- accent: high-visibility lime for positive signal and focus state
- text: warm white for primary, softened white-gray for secondary

Rules:

- do not use more than one bright accent in the core desk
- reserve lime for selected range, key gain signal, highlighted card, and major positive state
- keep negative/error color use minimal and isolated to true failures
- use muted neutral chips for passive metadata so status chips remain visually meaningful

### Typography

- large numeric typography for the primary value headline
- restrained sans-serif body typography
- slightly tighter tracking on brand and main numeric displays
- no decorative type beyond the brand mark
- all buttons, pills, tags, and badges use centered baseline alignment and single-line labels where possible
- titles and subtitles may truncate only when an inspector or expanded view can reveal the full value without breaking layout

### Shape and Surface

- large outer shell radius
- medium card radii
- low-border, high-mass cards
- shadows belong to the shell, not every internal panel
- spacing must feel mathematically aligned; avoid uneven gutters or soft floating misalignment
- all internal panes with potentially large datasets must have explicit bounded height and internal scrolling

## Controls and Interaction Standards

The previous chip-heavy controls were too soft and too unstable under hover. The approved interaction language uses more disciplined control groups inspired by professional charting and trading products.

### Control behavior rules

- hover, active, focus, and pressed states must never change control width, height, padding, line-height, border thickness, or text wrap behavior
- buttons and chips should communicate state through color, shadow, inset highlight, or opacity changes instead of geometry changes
- labels remain vertically centered at all states
- icon and text spacing remains fixed at all states
- if a label is too long for a control, the control must be redesigned rather than allowed to clip during hover

### Filter language

- segmented range controls
- grouped filter bars with compact dark buttons
- explicit active state using lime or brighter fill
- saved views and column controls grouped logically
- dense, centered labels with no vertical drift
- horizontal scrolling controls are allowed on mobile, but scrollbars must be visually hidden

## Dashboard UI Audit Requirements

A full visual QA pass is required across the public dashboard and operator dashboard.

The audit must specifically verify:

- button text alignment
- badge and pill centering
- card title and subtitle alignment
- operator labels and status text alignment
- chart label positioning
- padding consistency inside metrics, cards, inspectors, and control bars
- absence of horizontal overflow
- absence of clipped or ellipsized text in critical controls
- stable internal scrolling for dense sections

If a component cannot meet these standards in its current composition, it should be redesigned within the same visual system rather than patched superficially.

## Scanner Opportunity Surface

The scanner opportunity surface should stay compact and stable under density.

### Row design

- one compact row per candidate
- title block on the left with bounded width
- metrics in a clean two-column or two-by-two grid depending on breakpoint
- status badge and metadata chips in a stable side column
- chips wrap intentionally without colliding with badges or titles
- no metric labels may overlap or collapse into the title region

### Interaction behavior

- control pills above the scanner must keep fixed geometry on hover
- candidate rows may lift subtly on hover but must not change internal text alignment
- status badges remain short and standardized: `Tradable`, `Review`, `Manual`, `Held`, `Stale`

## Activity Atlas

Activity Atlas should be a compact operational console rather than a long storytelling feed.

### Purpose

Provide concise, scannable event history and live operational context without forcing the page to grow vertically as volume increases.

### Layout

- left or top scope controls for event domains
- category atlas for quick filtering
- center timeline pane with fixed height and internal scroll
- search and compact filter rail above the timeline

### Event design

Each event card should compress into three layers:

1. primary line
   - short event verb plus status
   - examples: `Route published - Ready`, `Order submitted - Pending`, `Manual venue - Waiting`
2. secondary meta line
   - venue path, relative time, and one key metric
   - examples: `Kalshi -> PredictIt - 12s ago - edge 18.4c`
3. tertiary metadata
   - compact chips for tags, reason class, collector source, audit state, or operator flag

### Language rules

- prefer terse operational verbs over sentence fragments
- use standardized statuses instead of free-form copy
- keep narratives to short support text only when necessary
- category descriptions must remain two lines or less

### Volume handling

- timeline pane must be height-bounded with internal scroll
- category pane must be independently scrollable
- event cards must stay visually compact under large result sets
- mobile must preserve the same event hierarchy without multi-line chaos

## Mapping Workspace

The mapping section must support thousands to hundreds of thousands of records without turning into an endless vertical page.

### Core model

The mapping section becomes a dedicated workbench with a fixed-height, three-pane layout:

1. Left rail: queue navigation and saved views
   - `Unmapped`
   - `Needs review`
   - `Conflict`
   - `Auto-trade eligible`
   - `Held`
   - `Recent changes`
2. Center pane: virtualized mapping console
   - sticky header
   - internal scroll only
   - dense selectable rows
   - search, sort, and quick filters
3. Right rail: mapping inspector
   - selected mapping details
   - venue comparisons
   - confidence explanation
   - review and action controls

### Behavior rules

- the page itself must not keep growing as more mappings load
- the row list must be bounded in height and scroll internally
- pagination alone is not sufficient; the UI must feel like a managed console, not a sequence of cards
- saved views and filters must let operators slice the queue instead of browsing the entire universe
- selection state must remain visually clear while the inspector updates

### Row design

Each mapping row should include:

- canonical event title
- matched venue pair or venue count
- confidence level
- review state
- freshness or last-updated signal
- note indicator / action state

Rules:

- rows stay one line tall where possible
- long event names truncate cleanly and reveal full detail in the inspector
- status language is standardized: `Ready`, `Review`, `Conflict`, `Held`, `Stale`
- metadata should read like a professional operations queue, not like stacked marketing cards

### Inspector design

The inspector should expose detail without expanding the list.

Sections may include:

- canonical event summary
- compared venue contracts
- confidence / conflict explanation
- operator notes
- action group

Inspector sections should be collapsible so dense detail remains available without visual noise.

## Operator Dashboard

The operator dashboard must look as polished as the public dashboard while remaining more utilitarian.

### Requirements

- operator labels, mode badges, and action buttons must align cleanly on their baselines
- card headers must maintain consistent title-to-badge spacing
- action rows must keep equal button heights and consistent wrapping rules
- dense operational cards may be redesigned if needed to prevent label collision or button truncation
- utility takes precedence over decorative symmetry, but visual sloppiness is not acceptable

## Responsive Behavior

### Desktop 1440+

- keep the large split layout: chart left, risk/trades right
- operational workbenches use bounded panes instead of page-height expansion
- navigation remains fully visible

### Tablet 768-1024

- stack the right rail below the chart as a full-width sheet
- keep top navigation simplified
- metrics compress into a two-column grid
- workbench inspectors may become slide-over or stacked panels
- mapping and Activity Atlas keep internal scrolling within their sections

### Mobile 390-480

- chart remains first and visually dominant
- range/filter controls become horizontally scrollable tabs with hidden scrollbar chrome
- right rail becomes a compressed bottom sheet below the chart
- metrics collapse into paired tiles
- secondary content becomes modern disclosure tiles with leading icons, short summaries, and clear chevron affordance
- mapping becomes a compact queue list plus bottom-sheet inspector
- Activity Atlas becomes a compact event queue with stable line wrapping and bounded panes
- primary section navigation moves to a bottom dock

### Responsive Rules

- no visible scrollbar chrome in horizontal control rows
- no clipped text or vertically off-center pills/buttons
- no overflowing cards, sheets, or inspectors
- all dense sections must move to internal scroll regions before they can grow page height
- gutters and padding should reduce proportionally, not abruptly

## Accessibility and UX Standards

- maintain strong contrast between primary text and dark surfaces
- keep touch targets comfortable on mobile even when compacted
- never rely on lime alone to communicate status
- preserve semantic grouping so operators can scan the page by section
- ensure critical information remains visible without hover
- support keyboard focus states that do not distort layout

## Implementation Notes

- implement layout stabilization before visual polish details
- fix hover-state geometry issues before redesigning button language
- treat mapping and Activity Atlas as managed consoles, not content feeds
- prefer denser structured rows over taller decorative cards in high-volume surfaces
- preserve the approved visual hierarchy even when real data density increases
- only change UI composition, content phrasing, and presentation behavior in this phase

## Acceptance Criteria

The redesign is successful when:

- desktop clearly matches the approved reference-inspired direction
- public and operator dashboards feel premium, dense, and intentional
- button, chip, and badge text stay aligned and untruncated in all interactive states
- Activity Atlas reads as a concise event console rather than a long scrolling feed
- mapping works as a bounded high-volume workspace rather than an endless list
- tablet and mobile keep the same identity without awkward shrinking or uncontrolled overflow
- no visible spacing, alignment, truncation, or hover-state geometry issues remain in the touched UI surfaces
- no visible scrollbar is present in horizontal mobile control rows
- the page height stays stable even when event or mapping volume becomes very large
