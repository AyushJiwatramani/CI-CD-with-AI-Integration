#!/usr/bin/env python3
"""
tools/get_impacted.py

Usage examples (CI):
  # (runs git diff origin/main...HEAD by default)
  python tools/get_impacted.py --orchestrator-url http://127.0.0.1:8000 --out tests.json

  # Use PR number (requires GITHUB_TOKEN and GITHUB_REPOSITORY env vars)
  python tools/get_impacted.py --pr 123 --github-token "$GITHUB_TOKEN" --repo "org/repo" --out tests.json

Options:
  --diff-range <range>    Git diff range to compute changed files (e.g., origin/main...HEAD)
  --pr <PR_NUMBER>        Use GitHub REST API to fetch PR files (needs token & repo)
  --repo <owner/repo>     Repo (e.g. myorg/myrepo). If not provided, tries GITHUB_REPOSITORY env.
  --orchestrator-url URL  Orchestrator base URL (default http://127.0.0.1:8000)
  --out PATH              Output JSON file path (default: tests.json)
  --fail-on-error         Exit non-zero when orchestrator or git fails (CI defaults to not fail)
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import List, Optional

try:
    import requests
except Exception:
    print("ERROR: 'requests' is required. Install with `pip install requests`", file=sys.stderr)
    sys.exit(2)

LOG = logging.getLogger("get_impacted")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def run_cmd(cmd: List[str], cwd: Optional[str] = None) -> str:
    try:
        out = subprocess.check_output(cmd, cwd=cwd).decode("utf-8", errors="ignore")
        return out
    except subprocess.CalledProcessError as e:
        LOG.debug("Command failed: %s", " ".join(cmd))
        raise


def git_changed_files(diff_range: str = "origin/main...HEAD") -> List[str]:
    """
    Use git to compute changed files between diff_range.
    Returns a list of file paths (relative).
    """
    # Try the primary diff range
    try:
        out = run_cmd(["git", "diff", "--name-only", diff_range])
        files = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if files:
            LOG.info("Found %d changed files using diff range '%s'", len(files), diff_range)
            return files
        # fallback to HEAD~1..HEAD
        LOG.info("No files found for diff range '%s', trying HEAD~1..HEAD", diff_range)
    except Exception as e:
        LOG.debug("git diff primary attempt failed: %s", e)

    # fallback attempts
    for rng in ("HEAD~1..HEAD", "HEAD^..HEAD"):
        try:
            out = run_cmd(["git", "diff", "--name-only", rng])
            files = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if files:
                LOG.info("Found %d changed files using fallback range '%s'", len(files), rng)
                return files
        except Exception:
            continue

    # final fallback: staged or uncommitted files (git status)
    try:
        out = run_cmd(["git", "status", "--porcelain"])
        files = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            # porcelain format: XY <path>
            parts = ln.split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1])
        if files:
            LOG.info("Found %d changed files from git status", len(files))
            return files
    except Exception:
        LOG.debug("git status fallback failed")

    LOG.info("No changed files detected via git")
    return []


def github_pr_changed_files(pr_number: int, repo: str, token: str) -> List[str]:
    """
    Fetch PR file list using the GitHub API. Handles pagination.
    Requires token.
    """
    if not token:
        raise RuntimeError("GitHub token is required to fetch PR files")

    owner_repo = repo
    api = f"https://api.github.com/repos/{owner_repo}/pulls/{pr_number}/files"
    LOG.info("Querying GitHub API for PR files: %s", api)
    per_page = 100
    page = 1
    filenames: List[str] = []
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    while True:
        params = {"per_page": per_page, "page": page}
        r = requests.get(api, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"GitHub API returned status {r.status_code}: {r.text}")
        chunk = r.json()
        if not isinstance(chunk, list):
            raise RuntimeError("Unexpected GitHub API response when fetching PR files")
        for item in chunk:
            if "filename" in item:
                filenames.append(item["filename"])
        if len(chunk) < per_page:
            break
        page += 1
        time.sleep(0.1)
    LOG.info("Got %d files from PR %s", len(filenames), pr_number)
    return filenames


def call_orchestrator(orchestrator_url: str, changed_files: List[str], timeout: int = 10) -> dict:
    """
    Call /predict-impact on orchestrator. Returns parsed JSON response.
    Raises RuntimeError on non-200 responses.
    """
    url = orchestrator_url.rstrip("/") + "/predict-impact"
    payload = {"changed_files": changed_files}
    headers = {"Content-Type": "application/json"}
    LOG.info("Calling orchestrator %s with %d changed files", url, len(changed_files))
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Error connecting to orchestrator: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"Orchestrator returned status {r.status_code}: {r.text}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Failed to parse orchestrator response: {e}")


def extract_jobs_from_response(resp: dict) -> List[str]:
    """
    Extract recommended jobs from orchestrator response.
    Fallbacks:
      - 'recommended_jobs' key (preferred)
      - 'predictions' -> ['component'] -> map to 'unit:<component>'
    """
    if not isinstance(resp, dict):
        return []
    if "recommended_jobs" in resp and isinstance(resp["recommended_jobs"], list):
        jobs = [str(x) for x in resp["recommended_jobs"] if x is not None]
        LOG.info("Orchestrator returned %d recommended_jobs", len(jobs))
        return jobs
    if "predictions" in resp and isinstance(resp["predictions"], list):
        comps = []
        for p in resp["predictions"]:
            if isinstance(p, dict) and "component" in p:
                comps.append(f"unit:{p['component']}")
        LOG.info("Derived %d jobs from predictions", len(comps))
        return comps
    LOG.info("No jobs found in orchestrator response")
    return []


def write_json_file(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    LOG.info("Wrote %s", path)


def main(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser()
    p.add_argument("--diff-range", default="origin/main...HEAD", help="git diff range to compute changed files")
    p.add_argument("--pr", type=int, help="Pull Request number (use GitHub API to fetch changed files)")
    p.add_argument("--repo", help="owner/repo (e.g. org/repo). If omitted uses GITHUB_REPOSITORY env")
    p.add_argument("--github-token", help="GitHub token used to fetch PR files. If omitted uses GITHUB_TOKEN env")
    p.add_argument("--orchestrator-url", default=os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8000"),
                   help="Orchestrator base URL (default http://127.0.0.1:8000)")
    p.add_argument("--out", default="tests.json", help="Output JSON file path")
    p.add_argument("--fail-on-error", action="store_true", help="Exit with non-zero code on error")
    p.add_argument("--no-fallback", action="store_true", help="Do not fallback to ['full'] on failure; fail instead")
    args = p.parse_args(argv)

    # Resolve repo and token defaults
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    token = args.github_token or os.environ.get("GITHUB_TOKEN")

    changed_files: List[str] = []
    try:
        if args.pr:
            if not repo:
                raise RuntimeError("Repo must be specified via --repo or GITHUB_REPOSITORY when using --pr")
            if not token:
                raise RuntimeError("GITHUB_TOKEN required when using --pr to call GitHub API")
            changed_files = github_pr_changed_files(args.pr, repo, token)
        else:
            changed_files = git_changed_files(args.diff_range)
    except Exception as e:
        LOG.warning("Failed to obtain changed files: %s", e)
        if args.fail_on_error:
            raise
        changed_files = []

    # If no changed files found, interpret as "full" or allow orchestrator to handle empty list
    if not changed_files:
        LOG.info("No changed files detected. Will ask orchestrator (it may default to full pipeline).")

    # Call orchestrator
    try:
        resp = call_orchestrator(args.orchestrator_url, changed_files)
        jobs = extract_jobs_from_response(resp)
        if not jobs:
            LOG.info("No jobs returned by orchestrator; defaulting to ['full']")
            jobs = ["full"]
    except Exception as e:
        LOG.error("Orchestrator call failed: %s", e)
        if args.fail_on_error or args.no_fallback:
            raise
        # fallback to conservative full-run
        LOG.info("Falling back to conservative ['full'] job")
        jobs = ["full"]

    # Write output
    write_json_file(args.out, jobs)
    # Also print to stdout (compact) so GH Action can capture if needed
    print(json.dumps(jobs))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        sys.exit(2)
