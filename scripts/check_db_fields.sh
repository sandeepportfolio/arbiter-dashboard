#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH

# Check actual DB columns for market_mappings
docker exec arbiter-postgres-prod psql -U arbiter -d arbiter_live -c "\d market_mappings" 2>&1

echo "=== Sample confirmed rows ==="
docker exec arbiter-postgres-prod psql -U arbiter -d arbiter_live -c "SELECT canonical_id, status, kalshi_market_id, polymarket_slug, kalshi_ticker, poly_slug FROM market_mappings WHERE status='confirmed' LIMIT 5;" 2>&1

echo "=== Sample candidate rows ==="
docker exec arbiter-postgres-prod psql -U arbiter -d arbiter_live -c "SELECT canonical_id, status, kalshi_market_id, polymarket_slug, kalshi_ticker, poly_slug, mapping_score FROM market_mappings WHERE status='candidate' ORDER BY mapping_score DESC LIMIT 5;" 2>&1

echo "=== Check all column names ==="
docker exec arbiter-postgres-prod psql -U arbiter -d arbiter_live -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='market_mappings' ORDER BY ordinal_position;" 2>&1
