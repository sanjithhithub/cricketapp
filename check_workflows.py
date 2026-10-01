"""Parse every GitHub Actions workflow.

A malformed workflow is not caught by pytest, ruff, or the local test suite.
GitHub rejects it at dispatch time and the run fails before any job starts,
with the error only visible in the Actions UI. Parsing here turns that into a
local failure with a line number.

Run as part of the lint step:
    python check_workflows.py
"""

import sys
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")


def main() -> int:
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not files:
        print("no workflow files found")
        return 1

    failures = 0
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"FAIL {path}: invalid YAML\n{exc}")
            failures += 1
            continue

        # Catch the other way a workflow is rejected: valid YAML that is not a
        # valid workflow shape.
        problems = []
        if not isinstance(data, dict):
            problems.append("top level is not a mapping")
        else:
            # PyYAML resolves the bare key `on:` to the boolean True, so accept
            # either spelling when confirming the trigger is present.
            if "on" not in data and True not in data:
                problems.append("no 'on:' trigger")
            if "jobs" not in data:
                problems.append("no 'jobs:' section")
            else:
                for name, job in data["jobs"].items():
                    if "runs-on" not in job:
                        problems.append(f"job '{name}' has no 'runs-on'")
                    if "steps" not in job:
                        problems.append(f"job '{name}' has no 'steps'")

        if problems:
            print(f"FAIL {path}:")
            for problem in problems:
                print(f"  - {problem}")
            failures += 1
        else:
            job_names = ", ".join(data["jobs"])
            print(f"OK   {path}: {len(data['jobs'])} jobs ({job_names})")

    if failures:
        print(f"\n{failures} workflow(s) invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
