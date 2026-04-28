#!/usr/bin/env python3
"""
Continuous validation runner for Arbiter.

Run all validation tests and output a summary.
Can be used as a pre-deploy gate or a continuous health check.

Usage:
    python tests/run_validation.py           # Run all tests once
    python tests/run_validation.py --watch   # Run in a loop every 60s
"""
import subprocess
import sys
import time
import argparse
from pathlib import Path


def run_tests():
    """Run the full test suite and return (passed, failed, output)."""
    test_dir = Path(__file__).parent
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_dir), "-v", "--tb=short", "-q"],
        capture_output=True,
        text=True,
        cwd=str(test_dir.parent),
    )
    return result.returncode == 0, result.stdout, result.stderr


def main():
    parser = argparse.ArgumentParser(description="Arbiter validation runner")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds")
    args = parser.parse_args()

    if args.watch:
        print(f"🔄 Continuous validation mode (every {args.interval}s)")
        print("=" * 60)
        while True:
            passed, stdout, stderr = run_tests()
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            status = "✅ ALL PASSED" if passed else "❌ FAILURES DETECTED"
            print(f"\n[{timestamp}] {status}")
            if not passed:
                print(stdout)
                print(stderr)
            time.sleep(args.interval)
    else:
        passed, stdout, stderr = run_tests()
        print(stdout)
        if stderr:
            print(stderr)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
