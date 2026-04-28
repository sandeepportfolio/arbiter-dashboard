#!/bin/bash
# Run the deep LLM verifier validation and save results to a file
cd ~/Documents/arbiter
/usr/bin/python3 tests/test_llm_verifier_deep.py > /tmp/llm_verifier_results.txt 2>&1
echo "DONE" >> /tmp/llm_verifier_results.txt
