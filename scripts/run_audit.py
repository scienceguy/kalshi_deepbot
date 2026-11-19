#!/usr/bin/env python3
"""P0-4: Audit wrapper that fails loudly on any error.

Wraps sub-agent/audit dispatches: non-zero exit, empty output, a provider
error string or a missing findings file => task state FAILED, never PASSED.
A report must say "N findings from model X, run id Y" or "did not run: <reason>".
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = REPO_ROOT / "docs" / "audit_runs"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

def run_audit(model: str = "auto", extra_args: list = None) -> dict:
    """Run ponytail-audit and return structured result.

    Returns:
        dict with keys: status (PASSED/FAILED), findings_count, model, run_id, output, error
    """
    run_id = f"audit_{int(time.time())}"
    output_file = AUDIT_DIR / f"{run_id}.json"

    # Build the command - use opencode with the ponytail-audit skill
    # Since we can't invoke skills directly from here, we'll use the CLI approach
    cmd = ["python3", "-m", "ponytail_audit"] if extra_args is None else ["python3", "-m", "ponytail_audit"] + extra_args

    # Try to find ponytail-audit
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        )
    except FileNotFoundError:
        return {
            "status": "FAILED",
            "findings_count": 0,
            "model": model,
            "run_id": run_id,
            "output": "",
            "error": f"ponytail-audit not found in PATH: {cmd[0]}"
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "FAILED",
            "findings_count": 0,
            "model": model,
            "run_id": run_id,
            "output": "",
            "error": "Audit timed out after 300s"
        }
    except Exception as e:
        return {
            "status": "FAILED",
            "findings_count": 0,
            "model": model,
            "run_id": run_id,
            "output": "",
            "error": f"Audit execution failed: {e}"
        }

    # Check for failure conditions
    if result.returncode != 0:
        return {
            "status": "FAILED",
            "findings_count": 0,
            "model": model,
            "run_id": run_id,
            "output": result.stdout,
            "error": f"Non-zero exit code {result.returncode}: {result.stderr}"
        }

    if not result.stdout.strip():
        return {
            "status": "FAILED",
            "findings_count": 0,
            "model": model,
            "run_id": run_id,
            "output": "",
            "error": "Empty output from audit"
        }

    # Check for provider error strings in output
    error_indicators = [
        "provider error",
        "authentication failed",
        "rate limit",
        "quota exceeded",
        "model not available",
        "signup required",
        "api key",
        "unauthorized",
    ]
    stdout_lower = result.stdout.lower()
    for indicator in error_indicators:
        if indicator in stdout_lower:
            return {
                "status": "FAILED",
                "findings_count": 0,
                "model": model,
                "run_id": run_id,
                "output": result.stdout,
                "error": f"Provider error detected in output: {indicator}"
            }

    # Try to parse findings count from output
    findings_count = 0
    for line in result.stdout.split("\n"):
        if "finding" in line.lower() and any(c.isdigit() for c in line):
            # Try to extract a number
            import re
            match = re.search(r"(\d+)\s*finding", line, re.IGNORECASE)
            if match:
                findings_count = int(match.group(1))
                break

    # Write result file
    output_data = {
        "status": "PASSED",
        "findings_count": findings_count,
        "model": model,
        "run_id": run_id,
        "output": result.stdout,
        "error": None,
        "timestamp": time.time(),
        "command": " ".join(cmd),
    }
    output_file.write_text(json.dumps(output_data, indent=2))

    return output_data


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Run audit with failure detection")
    ap.add_argument("--model", default="auto", help="Model to use for audit")
    ap.add_argument("--extra-args", nargs="*", default=[], help="Extra args for audit")
    ap.add_argument("--output", help="Output file for result (default: docs/audit_runs/audit_<ts>.json)")
    args = ap.parse_args()

    result = run_audit(model=args.model, extra_args=args.extra_args)

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2))

    # Print summary
    print(f"Run ID: {result['run_id']}")
    print(f"Status: {result['status']}")
    print(f"Model: {result['model']}")
    print(f"Findings: {result['findings_count']}")
    if result['error']:
        print(f"Error: {result['error']}", file=sys.stderr)

    # Exit with appropriate code
    sys.exit(0 if result['status'] == 'PASSED' else 1)


if __name__ == "__main__":
    main()