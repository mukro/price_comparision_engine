#!/usr/bin/env python3
"""Quick test script for the Data Quality Agent."""
import sys
sys.path.insert(0, '/app')

from agent.main import run_entity_resolution, run_selector_health_check, run_full_pipeline

print("=" * 60)
print("PCE Data Quality Agent — Quick Test")
print("=" * 60)

# Test 1: Entity Resolution (small batch)
print("
[TEST 1] Entity Resolution (batch=5)")
result = run_entity_resolution(batch_size=5)
print(f"Result: {result}")

# Test 2: Selector Health
print("
[TEST 2] Selector Health Check")
result = run_selector_health_check()
print(f"Result: {result}")

# Test 3: Full Pipeline
print("
[TEST 3] Full Pipeline (batch=5)")
result = run_full_pipeline(batch_size=5)
print(f"Result: {result}")

print("
" + "=" * 60)
print("All tests completed. Check logs for details.")
print("=" * 60)
