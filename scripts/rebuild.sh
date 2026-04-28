#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH
cd /Users/rentamac/Documents/arbiter

echo "$(date): Starting build..." > /tmp/arbiter-build.log

# Build
docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod >> /tmp/arbiter-build.log 2>&1
echo "$(date): Build done" >> /tmp/arbiter-build.log

# Restart
docker compose -f docker-compose.prod.yml --env-file .env.production up -d arbiter-api-prod >> /tmp/arbiter-build.log 2>&1
echo "$(date): Restart done" >> /tmp/arbiter-build.log

# Restart LLM verifier sidecar
pkill -f llm_verifier_service 2>/dev/null
sleep 2
nohup python3 scripts/llm_verifier_service.py --port 8079 > /tmp/llm_verifier.log 2>&1 &
echo "$(date): Verifier restarted" >> /tmp/arbiter-build.log
echo "ALL_DONE" >> /tmp/arbiter-build.log
