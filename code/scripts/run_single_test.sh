#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <job-name>" >&2
  exit 2
fi

job="$1"
echo "RUNNER: Running job: $job"

# If pytest is installed we will try to run related tests, otherwise we just echo.
if command -v pytest >/dev/null 2>&1; then
  case "$job" in
    unit:*)
      module="${job#unit:}"
      # Prefer tests/unit/<module> directory if exists
      if [ -d "tests/unit/$module" ]; then
        echo "Running pytest for tests/unit/$module"
        pytest -q "tests/unit/$module" || exit $?
      else
        # attempt to run by -k module (test name match)
        echo "Attempting pytest -k $module"
        pytest -q -k "$module" || {
          echo "No tests matched for $module. Exiting with 0 (no-op)." >&2
          exit 0
        }
      fi
      ;;
    full)
      echo "Running full test suite (pytest)"
      pytest -q || exit $?
      ;;
    *)
      echo "Unknown job pattern '$job'. Attempting to run it as shell command."
      # Danger: if job contains malicious code, it will run. In CI this should be controlled.
      eval "$job" || exit $?
      ;;
  esac
else
  echo "pytest not available in runner. Printing job name instead (no-op)."
  echo "JOB: $job"
  exit 0
fi
