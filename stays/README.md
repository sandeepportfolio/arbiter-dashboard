# Lúmen Stays — boutique direct-booking site

A high-fidelity, fully responsive booking website for the five real Airbnb
listings across Las Colinas and Richardson. Self-contained static site — no
build step, no backend, no external dependencies.

## View it

```bash
cd stays
python3 -m http.server 8787
# open http://localhost:8787/
```

Or open `stays/index.html` through any static host. Everything (HTML, CSS, JS,
images) is local.

## Structure

| File | Purpose |
|------|---------|
| `index.html` | Page shell: hero, collection, experience, comforts, footer, modals |
| `styles.css` | All styling, animation and responsive rules |
| `data.js` | Listing content — titles, occupancy, descriptions, **full grouped amenities**, house rules, USPs, real Airbnb URLs, photo orderings |
| `app.js` | Rendering, scroll/motion, gallery lightbox, deep-link routing, and the full booking flow |
| `assets/img/s296`, `s397`, `s144` | Optimised real photos, stored once per source unit |

## What's included

- **All 5 listings**, each with its own personality and accent colour.
- **Real photos only** — the host's own edited property photos (no stock, no
  AI). AI-generated files in the source folders (`ChatGPT Image…`,
  `Gemini_Generated…`, `Generated Image…`) were deliberately excluded.
- Real occupancy, sanitised descriptions, and the **complete amenity lists**
  grouped into categories.
- A **working booking CTA on every listing**: date + guest + pet selection,
  live price estimate, a request-to-book modal with validation and a success
  state, a pre-filled email fallback, and a direct "Book on Airbnb" link to the
  real listing.
- Lightbox gallery, deep links (`#stay-<id>`), working back button, sticky
  mobile booking bar, scroll-reveal motion, and `prefers-reduced-motion`
  support.
- **Zero console errors.**

## Photo sourcing & mapping (please review)

Airbnb is blocked by this environment's network policy, so the official listing
photos could not be fetched. Instead, per your instruction, photos were pulled
from **your Google Drive** edited photo sets and matched to listings by content.

There are **3 photographed units (296, 397, 144) for 5 listings**, so two sets
are shared across near-identical units. Each shared listing leads with a
different hero image and a different photo order.

| # | Listing | Photo set | Notes |
|---|---------|-----------|-------|
| 1 | Cozy Designer Suite | **296** | Bright designer set with mini-golf + game corner |
| 2 | Designer Game Suite | **397** | Candles, round mirror, mini-hoop — strong match |
| 3 | Luxury Lake View Suite | **296** (shared with #1) | Different hero/order; see flag below |
| 4 | Luxury Executive Living | **144** | Fireplace + board games + star ceiling — exact match (5 photos) |
| 5 | Stunning Lake Views | **397** (shared with #2) | Leads on the L-shaped sectional |

### Flags for your review
1. **Shared sets:** #1/#3 share unit 296; #2/#5 share unit 397. If you have
   distinct photos for each unit, drop them in and I'll split them.
2. **Lake/balcony photos:** none of the three sets contains a clear lake or
   balcony shot, so #3 and #5 currently show real interiors only. The copy still
   describes the (real) lake views — add a balcony/lake photo to complete them.
3. **Unit 144** has only 5 real photos; the gallery is intentionally sized to
   look great with fewer images.
4. **Prices** (`priceFrom`) are illustrative starting rates — update the
   `priceFrom` values in `data.js`. Final booking completes on Airbnb.
5. **Brand** ("Lúmen Stays") and the contact email (`stays@lumen.house`) are
   placeholders — change them in `index.html` / `app.js` when you have the real
   ones.

## Adding or replacing photos

1. Drop optimised JPGs into `assets/img/s296` (or `s397` / `s144`), numbered
   `01.jpg`, `02.jpg`, …
2. Add a caption for each new number in the `CAPTIONS` map in `data.js`.
3. Reference the numbers in that listing's `gallery` array (first entry is the
   hero shown on the card and detail page).
