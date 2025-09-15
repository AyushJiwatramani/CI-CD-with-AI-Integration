#!/usr/bin/env python3
import requests, json, subprocess, sys
# find changed files between HEAD and origin/main
def get_changed_files():
    out = subprocess.check_output(["git","diff","--name-only","origin/main...HEAD"]).decode().strip()
    return [l for l in out.splitlines() if l]

if __name__ == "__main__":
    files = get_changed_files()
    payload = {"changed_files": files}
    r = requests.post("http://orchestrator:8000/predict-impact", json=payload)
    out = r.json()
    # write tests list
    tests = out.get("recommended_jobs", [])
    with open("tests.json","w") as fh:
        json.dump(tests, fh)
    print(json.dumps(tests))
