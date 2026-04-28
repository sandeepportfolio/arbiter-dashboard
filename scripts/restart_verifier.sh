#!/bin/bash
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH
pkill -f llm_verifier_service 2>/dev/null
sleep 2
cd /Users/rentamac/Documents/arbiter
nohup python3 scripts/llm_verifier_service.py --port 8079 > /tmp/llm_verifier.log 2>&1 &
echo "Verifier PID: $!"
