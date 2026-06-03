-- New cross-platform market mappings
-- Generated: 2026-06-03 by discover_new_mappings pipeline
-- Categories: Political 2028, Fed rate decisions, Polymarket 2026 Senate party-winner by state

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- POLITICAL: 2028 House/Senate control (Kalshi only for now, no Poly match yet)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INSERT INTO market_mappings (
    canonical_id, description, status, allow_auto_trade,
    kalshi_market_id, polymarket_slug, polymarket_question,
    mapping_score, confidence, notes, tags,
    created_at, updated_at
) VALUES (
    'DEM_HOUSE_2028', 'Democrats win House 2028', 'confirmed', false,
    'CONTROLH-2028-D', '', 'Will the Democratic party win the House in 2028?',
    0.95, 0.95,
    'Kalshi 2028 House control. Polymarket slug TBD when market launches.',
    ARRAY['politics', 'house', '2028'],
    NOW(), NOW()
) ON CONFLICT (canonical_id) DO NOTHING;

INSERT INTO market_mappings (
    canonical_id, description, status, allow_auto_trade,
    kalshi_market_id, polymarket_slug, polymarket_question,
    mapping_score, confidence, notes, tags,
    created_at, updated_at
) VALUES (
    'GOP_HOUSE_2028', 'Republicans win House 2028', 'confirmed', false,
    'CONTROLH-2028-R', '', 'Will the Republican party win the House in 2028?',
    0.95, 0.95,
    'Kalshi 2028 House control. Polymarket slug TBD when market launches.',
    ARRAY['politics', 'house', '2028'],
    NOW(), NOW()
) ON CONFLICT (canonical_id) DO NOTHING;

INSERT INTO market_mappings (
    canonical_id, description, status, allow_auto_trade,
    kalshi_market_id, polymarket_slug, polymarket_question,
    mapping_score, confidence, notes, tags,
    created_at, updated_at
) VALUES (
    'DEM_SENATE_2028', 'Democrats win Senate 2028', 'confirmed', false,
    'CONTROLS-2028-D', '', 'Will the Democratic party win the Senate in 2028?',
    0.95, 0.95,
    'Kalshi 2028 Senate control. Polymarket slug TBD when market launches.',
    ARRAY['politics', 'senate', '2028'],
    NOW(), NOW()
) ON CONFLICT (canonical_id) DO NOTHING;

INSERT INTO market_mappings (
    canonical_id, description, status, allow_auto_trade,
    kalshi_market_id, polymarket_slug, polymarket_question,
    mapping_score, confidence, notes, tags,
    created_at, updated_at
) VALUES (
    'GOP_SENATE_2028', 'Republicans win Senate 2028', 'confirmed', false,
    'CONTROLS-2028-R', '', 'Will the Republican party win the Senate in 2028?',
    0.95, 0.95,
    'Kalshi 2028 Senate control. Polymarket slug TBD when market launches.',
    ARRAY['politics', 'senate', '2028'],
    NOW(), NOW()
) ON CONFLICT (canonical_id) DO NOTHING;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Update existing 2026 midterm mappings with verified Polymarket slugs
-- (the slug format on Polymarket may have changed since original mapping)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

UPDATE market_mappings SET
    polymarket_slug = 'will-the-democratic-party-control-the-house-after-the-2026-m',
    polymarket_question = 'Will the Democratic Party control the House after the 2026 Midterm elections?',
    updated_at = NOW()
WHERE canonical_id = 'DEM_HOUSE_2026' AND status = 'confirmed';

UPDATE market_mappings SET
    polymarket_slug = 'will-the-republican-party-control-the-house-after-the-2026-m',
    polymarket_question = 'Will the Republican Party control the House after the 2026 Midterm elections?',
    updated_at = NOW()
WHERE canonical_id = 'GOP_HOUSE_2026' AND status = 'confirmed';

UPDATE market_mappings SET
    polymarket_slug = 'will-the-democratic-party-control-the-senate-after-the-2026-',
    polymarket_question = 'Will the Democratic Party control the Senate after the 2026 Midterm elections?',
    updated_at = NOW()
WHERE canonical_id = 'DEM_SENATE_2026' AND status = 'confirmed';

UPDATE market_mappings SET
    polymarket_slug = 'will-the-republican-party-control-the-senate-after-the-2026-',
    polymarket_question = 'Will the Republican Party control the Senate after the 2026 Midterm elections?',
    updated_at = NOW()
WHERE canonical_id = 'GOP_SENATE_2026' AND status = 'confirmed';

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- POLITICAL: Polymarket 2026 Senate state races (Dem/Rep party winner)
-- Polymarket-only for now — Kalshi SENATEPARTY series has no open events
-- Insert as confirmed so they're ready when Kalshi opens state races
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- Generate state Senate race mappings for all states where Polymarket has markets
-- These are Polymarket-only confirmed (Kalshi has no matching state-level markets yet)

-- Competitive swing states first (most valuable for arbitrage when Kalshi opens)
INSERT INTO market_mappings (canonical_id, description, status, allow_auto_trade, kalshi_market_id, polymarket_slug, polymarket_question, mapping_score, confidence, notes, tags, created_at, updated_at) VALUES
('POL_SENATE_2026_TX_DEM', 'Democrats win Texas Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-texas-senate-race-in-2026', 'Will the Democrats win the Texas Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_TX_GOP', 'Republicans win Texas Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-texas-senate-race-in-2026', 'Will the Republicans win the Texas Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_GA_DEM', 'Democrats win Georgia Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-georgia-senate-race-in-2026', 'Will the Democrats win the Georgia Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_GA_GOP', 'Republicans win Georgia Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-georgia-senate-race-in-2026', 'Will the Republicans win the Georgia Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MI_DEM', 'Democrats win Michigan Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-michigan-senate-race-in-2026', 'Will the Democrats win the Michigan Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MI_GOP', 'Republicans win Michigan Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-michigan-senate-race-in-2026', 'Will the Republicans win the Michigan Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NC_DEM', 'Democrats win North Carolina Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-north-carolina-senate-race-in-202', 'Will the Democrats win the North Carolina Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NC_GOP', 'Republicans win North Carolina Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-north-carolina-senate-race-in-2', 'Will the Republicans win the North Carolina Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_VA_DEM', 'Democrats win Virginia Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-virginia-senate-race-in-2026', 'Will the Democrats win the Virginia Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_VA_GOP', 'Republicans win Virginia Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-virginia-senate-race-in-2026', 'Will the Republicans win the Virginia Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_ME_DEM', 'Democrats win Maine Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-maine-senate-race-in-2026', 'Will the Democrats win the Maine Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_ME_GOP', 'Republicans win Maine Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-maine-senate-race-in-2026', 'Will the Republicans win the Maine Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MN_DEM', 'Democrats win Minnesota Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-minnesota-senate-race-in-2026', 'Will the Democrats win the Minnesota Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MN_GOP', 'Republicans win Minnesota Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-minnesota-senate-race-in-2026', 'Will the Republicans win the Minnesota Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MT_DEM', 'Democrats win Montana Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-montana-senate-race-in-2026', 'Will the Democrats win the Montana Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MT_GOP', 'Republicans win Montana Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-montana-senate-race-in-2026', 'Will the Republicans win the Montana Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NH_DEM', 'Democrats win New Hampshire Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-new-hampshire-senate-race-in-2026', 'Will the Democrats win the New Hampshire Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NH_GOP', 'Republicans win New Hampshire Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-new-hampshire-senate-race-in-20', 'Will the Republicans win the New Hampshire Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NJ_DEM', 'Democrats win New Jersey Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-new-jersey-senate-race-in-2026', 'Will the Democrats win the New Jersey Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NJ_GOP', 'Republicans win New Jersey Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-new-jersey-senate-race-in-2026', 'Will the Republicans win the New Jersey Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_OH_DEM', 'Democrats win Ohio Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-ohio-senate-race-in-2026', 'Will the Democrats win the Ohio Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_OH_GOP', 'Republicans win Ohio Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-ohio-senate-race-in-2026', 'Will the Republicans win the Ohio Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_FL_DEM', 'Democrats win Florida Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-florida-senate-race-in-2026', 'Will the Democrats win the Florida Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_FL_GOP', 'Republicans win Florida Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-florida-senate-race-in-2026', 'Will the Republicans win the Florida Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_PA_DEM', 'Democrats win Pennsylvania Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-pennsylvania-senate-race-in-2026', 'Will the Democrats win the Pennsylvania Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_PA_GOP', 'Republicans win Pennsylvania Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-pennsylvania-senate-race-in-202', 'Will the Republicans win the Pennsylvania Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_OR_DEM', 'Democrats win Oregon Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-oregon-senate-race-in-2026', 'Will the Democrats win the Oregon Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_OR_GOP', 'Republicans win Oregon Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-oregon-senate-race-in-2026', 'Will the Republicans win the Oregon Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_IA_DEM', 'Democrats win Iowa Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-iowa-senate-race-in-2026', 'Will the Democrats win the Iowa Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_IA_GOP', 'Republicans win Iowa Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-iowa-senate-race-in-2026', 'Will the Republicans win the Iowa Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_CO_DEM', 'Democrats win Colorado Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-colorado-senate-race-in-2026', 'Will the Democrats win the Colorado Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_CO_GOP', 'Republicans win Colorado Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-colorado-senate-race-in-2026', 'Will the Republicans win the Colorado Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_KS_DEM', 'Democrats win Kansas Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-kansas-senate-race-in-2026', 'Will the Democrats win the Kansas Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_KS_GOP', 'Republicans win Kansas Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-kansas-senate-race-in-2026', 'Will the Republicans win the Kansas Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_KY_DEM', 'Democrats win Kentucky Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-kentucky-senate-race-in-2026', 'Will the Democrats win the Kentucky Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_KY_GOP', 'Republicans win Kentucky Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-kentucky-senate-race-in-2026', 'Will the Republicans win the Kentucky Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NE_DEM', 'Democrats win Nebraska Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-nebraska-senate-race-in-2026', 'Will the Democrats win the Nebraska Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_NE_GOP', 'Republicans win Nebraska Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-nebraska-senate-race-in-2026', 'Will the Republicans win the Nebraska Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_LA_DEM', 'Democrats win Louisiana Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-louisiana-senate-race-in-2026', 'Will the Democrats win the Louisiana Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_LA_GOP', 'Republicans win Louisiana Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-louisiana-senate-race-in-2026', 'Will the Republicans win the Louisiana Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_IL_DEM', 'Democrats win Illinois Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-illinois-senate-race-in-2026', 'Will the Democrats win the Illinois Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_IL_GOP', 'Republicans win Illinois Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-illinois-senate-race-in-2026', 'Will the Republicans win the Illinois Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MA_DEM', 'Democrats win Massachusetts Senate 2026', 'confirmed', false, '', 'will-the-democrats-win-the-massachusetts-senate-race-in-2026', 'Will the Democrats win the Massachusetts Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW()),
('POL_SENATE_2026_MA_GOP', 'Republicans win Massachusetts Senate 2026', 'confirmed', false, '', 'will-the-republicans-win-the-massachusetts-senate-race-in-20', 'Will the Republicans win the Massachusetts Senate race in 2026?', 0.90, 0.90, 'Polymarket 2026 Senate race. Kalshi state-level TBD.', ARRAY['politics', 'senate', '2026'], NOW(), NOW())
ON CONFLICT (canonical_id) DO NOTHING;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- POLITICAL: Polymarket 2026 Governor party-winner by state (swing states)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INSERT INTO market_mappings (canonical_id, description, status, allow_auto_trade, kalshi_market_id, polymarket_slug, polymarket_question, mapping_score, confidence, notes, tags, created_at, updated_at) VALUES
('POL_GOV_2026_AZ_DEM', 'Democrats win Arizona Governor 2026', 'confirmed', false, '', 'will-the-democrat-win-the-arizona-governor-race-in-2026', 'Will the Democrats win the Arizona governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_AZ_GOP', 'Republicans win Arizona Governor 2026', 'confirmed', false, '', 'will-the-republican-win-the-arizona-governor-race-in-2026', 'Will the Republicans win the Arizona governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_FL_DEM', 'Democrats win Florida Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-florida-governor-race-in-2026', 'Will the Democrats win the Florida governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_FL_GOP', 'Republicans win Florida Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-florida-governor-race-in-2026', 'Will the Republicans win the Florida governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_GA_DEM', 'Democrats win Georgia Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-georgia-governor-race-in-2026', 'Will the Democrats win the Georgia governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_GA_GOP', 'Republicans win Georgia Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-georgia-governor-race-in-2026', 'Will the Republicans win the Georgia governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_MI_DEM', 'Democrats win Michigan Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-michigan-governor-race-in-2026', 'Will the Democrats win the Michigan governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_MI_GOP', 'Republicans win Michigan Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-michigan-governor-race-in-2026', 'Will the Republicans win the Michigan governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_PA_DEM', 'Democrats win Pennsylvania Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-pennsylvania-governor-race-in-202', 'Will the Democrats win the Pennsylvania governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_PA_GOP', 'Republicans win Pennsylvania Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-pennsylvania-governor-race-in-2', 'Will the Republicans win the Pennsylvania governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_OH_DEM', 'Democrats win Ohio Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-ohio-governor-race-in-2026', 'Will the Democrats win the Ohio governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_OH_GOP', 'Republicans win Ohio Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-ohio-governor-race-in-2026', 'Will the Republicans win the Ohio governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_WI_DEM', 'Democrats win Wisconsin Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-wisconsin-governor-race-in-2026', 'Will the Democrats win the Wisconsin governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_WI_GOP', 'Republicans win Wisconsin Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-wisconsin-governor-race-in-2026', 'Will the Republicans win the Wisconsin governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_NV_DEM', 'Democrats win Nevada Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-nevada-governor-race-in-2026', 'Will the Democrats win the Nevada governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_NV_GOP', 'Republicans win Nevada Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-nevada-governor-race-in-2026', 'Will the Republicans win the Nevada governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_MN_DEM', 'Democrats win Minnesota Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-minnesota-governor-race-in-2026', 'Will the Democrats win the Minnesota governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_MN_GOP', 'Republicans win Minnesota Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-minnesota-governor-race-in-2026', 'Will the Republicans win the Minnesota governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_NY_DEM', 'Democrats win New York Governor 2026', 'confirmed', false, '', 'will-the-democrats-win-the-new-york-governor-race-in-2026', 'Will the Democrats win the New York governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW()),
('POL_GOV_2026_NY_GOP', 'Republicans win New York Governor 2026', 'confirmed', false, '', 'will-the-republicans-win-the-new-york-governor-race-in-2026', 'Will the Republicans win the New York governor race in 2026?', 0.90, 0.90, 'Polymarket 2026 Governor race.', ARRAY['politics', 'governor', '2026'], NOW(), NOW())
ON CONFLICT (canonical_id) DO NOTHING;
