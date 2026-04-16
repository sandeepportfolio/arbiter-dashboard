# Arbiter Desk Redesign Design

Date: 2026-04-16

## Goal

Redesign the Arbiter trading dashboard for high-volume operation where hundreds or thousands of trades, scans, alerts, and historical events must remain understandable at a glance. The new experience should feel premium and current while prioritizing decision speed, operator confidence, and low cognitive load.

## Outcome

The approved direction is a `blotter-first` trading desk rendered in a reference-inspired visual system:

- soft sage application backdrop outside the product shell
- monolithic black primary workspace with large radii and minimal chrome
- lime accent used sparingly for live focus, positive state, and selected controls
- oversized left-side primary chart as the visual anchor
- right-side risk and recent-trades rail for secondary context
- compact metrics strip below the chart
- strong spacing discipline, quiet typography, and dense-but-readable cards

This is not a generic crypto wallet clone. The composition follows the reference image, but the content remains native to Arbiter: route activity, portfolio risk, opportunity scanning, and execution state.

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
4. Bottom stat strip
   - realized P&L
   - unrealized P&L
   - projected growth
   - net change

### Trading Surface Mapping

Even when the visual hierarchy resembles the reference, the actual product behavior remains blotter-first:

- the large left chart represents live trade activity, portfolio value, or route performance
- the right rail represents recent trades, trade outcomes, or risk posture
- deeper operational surfaces such as scanner details, incident queues, mappings, and collectors live behind navigation or lower-priority docked modules
- any row-level action surface should remain tied to the currently selected route instead of becoming a separate disconnected page

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

### Typography

- large numeric typography for the primary value headline
- restrained sans-serif body typography
- slightly tighter tracking on brand and main numeric displays
- no decorative type beyond the brand mark
- all buttons and pills must use centered baseline alignment and single-line labels

### Shape and Surface

- large outer shell radius
- medium card radii
- low-border, high-mass cards
- shadows belong to the shell, not every internal panel
- spacing must feel mathematically aligned; avoid uneven gutters or soft floating misalignment

## Controls and Filters

The previous chip-heavy controls were too soft. The approved interaction language uses more disciplined control groups inspired by TradingView:

- segmented range controls
- grouped filter bars with compact dark buttons
- explicit active state using lime or brighter fill
- saved views and column controls grouped logically
- dense, centered labels with no vertical drift
- horizontal scrolling controls are allowed on mobile, but scrollbars must be visually hidden

## Responsive Behavior

### Desktop 1440+

- keep the large split layout: chart left, risk/trades right
- bottom stat strip stays in one row
- navigation remains fully visible

### Tablet 768-1024

- stack the right rail below the chart as a full-width sheet
- keep top navigation simplified
- metrics compress into a two-column grid
- trade cards compress into a two-column grid

### Mobile 390-480

- chart remains first and visually dominant
- range/filter controls become horizontally scrollable tabs with hidden scrollbar chrome
- right rail becomes a compressed bottom sheet below the chart
- metrics collapse into paired tiles
- trade cards collapse into two smaller cards
- secondary content becomes accordion sections
- primary section navigation moves to a bottom dock

### Responsive Rules

- no visible scrollbar chrome in horizontal control rows
- no clipped text or vertically off-center pills/buttons
- no overflowing cards or sheets
- all mobile content must fit within the mock device frame without accidental vertical overflow
- gutters and padding should reduce proportionally, not abruptly

## Component Priorities

### Must Keep

- large chart-led hero
- portfolio risk module
- recent trades card cluster
- bottom metrics strip
- premium reference-inspired composition

### Must Improve

- alignment and spacing precision
- button and filter label centering
- compactness of mobile cards and tiles
- responsive adaptation without losing visual identity

### Must Avoid

- endless dashboard scrolling
- card noise
- multiple competing accents
- cramped or inconsistent spacing
- visible mobile scrollbars inside the UI

## Accessibility and UX Standards

- maintain strong contrast between primary text and dark surfaces
- keep touch targets comfortable on mobile even when compacted
- never rely on lime alone to communicate status
- preserve semantic grouping so operators can scan the page by section
- ensure critical information remains visible without hover

## Implementation Notes

- implement the visual shell first, then wire live data into the approved zones
- use a single responsive system rather than separate disconnected layouts
- prioritize a faithful desktop implementation, then tablet/mobile compression rules
- preserve the approved hierarchy even when real data density increases

## Acceptance Criteria

The redesign is successful when:

- desktop clearly matches the approved reference-inspired direction
- the experience feels premium, calm, and high-value rather than generic admin UI
- tablet and mobile keep the same identity without awkward shrinking
- all filter controls feel intentional and professional
- no visible spacing or alignment issues remain in the shell
- no visible scrollbar is present in the mobile control row
- the layout supports dense trade information without confusion
