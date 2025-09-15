# code/src/orchestrator/main.py
"""FastAPI orchestrator - robust imports for local runs.

This file ensures the local 'code/src' folder is on sys.path so that imports like
`from analyzer.static import ...` work when running uvicorn from repo root.

Recommended to use this during development / hackathon.
"""
from __future__ import annotations

import os
import sys
import logging

# ---------------------------
# Ensure repo 'src' is on sys.path
# ---------------------------
# file is: <repo-root>/code/src/orchestrator/main.py
_this_dir = os.path.dirname(os.path.abspath(__file__))
# src_dir -> <repo-root>/code/src
_src_dir = os.path.abspath(os.path.join(_this_dir, ".."))
if _src_dir not in sys.path:
    # Insert at front so local packages are preferred during development.
    sys.path.insert(0, _src_dir)

# Now imports that expect 'analyzer' to be a top-level package will work.
try:
    from analyzer.static import get_impacted_modules, map_files_to_modules
except Exception as exc:
    # Provide a friendly error with debugging tips.
    msg = (
        "Failed to import 'analyzer' package. This usually means Python's import path "
        "doesn't include the 'code/src' directory.\n\n"
        f"sys.path (first 10 entries): {sys.path[:10]!r}\n\n"
        "Quick fixes:\n"
        " - Run uvicorn from repo root and use the patched main.py (this file adds code/src to sys.path),\n"
        " - OR set PYTHONPATH=code/src and run `uvicorn orchestrator.main:app --reload`,\n"
        " - OR add __init__.py files to create packages (code/, code/src/, code/src/analyzer/, code/src/orchestrator/).\n\n"
        "Original import error: "
    )
    raise ImportError(msg) from exc

# ---------------------------
# FastAPI app
# ---------------------------
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

logger = logging.getLogger("orchestrator")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Impact-Orchestrator", version="0.1.0")


DEFAULT_REPO_ROOT = os.environ.get("REPO_ROOT", ".")
DEFAULT_SRC_ROOTS = ["src", "packages", "lib"]


class PredictRequest(BaseModel):
    changed_files: Optional[List[str]] = Field(
        default=None, description="List of changed file paths (relative to repo root)"
    )
    repo_root: Optional[str] = Field(default=None, description="Path to repo root (optional)")
    group_depth: Optional[int] = Field(default=2, description="Module grouping depth")
    max_hops: Optional[int] = Field(default=1, description="Dependency hops for impact analysis")


class ComponentPrediction(BaseModel):
    component: str
    confidence: float
    reason: str


class PredictResponse(BaseModel):
    predictions: List[ComponentPrediction]
    recommended_jobs: List[str]


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Impact-Orchestrator"}


@app.post("/predict-impact", response_model=PredictResponse)
async def predict_impact(req: PredictRequest):
    repo_root = req.repo_root or DEFAULT_REPO_ROOT
    group_depth = req.group_depth if req.group_depth is not None else 2
    max_hops = req.max_hops if req.max_hops is not None else 1

    if not req.changed_files or not isinstance(req.changed_files, list) or len(req.changed_files) == 0:
        raise HTTPException(status_code=400, detail="Please provide non-empty 'changed_files' list in body.")

    changed_files = [str(x).strip() for x in req.changed_files if isinstance(x, str) and x.strip()]
    if not changed_files:
        raise HTTPException(status_code=400, detail="No valid file paths found in 'changed_files' list.")

    try:
        preds = get_impacted_modules(
            changed_files=changed_files,
            repo_root=repo_root,
            src_roots=DEFAULT_SRC_ROOTS,
            group_depth=group_depth,
            max_hops=max_hops,
        )
    except Exception as e:
        logger.exception("Error computing impact: %s", e)
        raise HTTPException(status_code=500, detail="Internal error computing impact.")

    recommended_jobs = [f"unit:{p['component']}" for p in preds]

    response = {
        "predictions": preds,
        "recommended_jobs": recommended_jobs,
    }
    return response


@app.post("/webhook")
async def webhook_handler(request: Request, x_github_event: Optional[str] = Header(None)):
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if payload and isinstance(payload, dict) and "changed_files" in payload:
        try:
            body = PredictRequest(**{"changed_files": payload["changed_files"]})
            return await predict_impact(body)
        except Exception as e:
            logger.exception("Webhook -> predict error: %s", e)
            raise HTTPException(status_code=400, detail="Invalid webhook payload for changed_files.")
    else:
        hint = {
            "message": "Received webhook.",
            "event": x_github_event,
            "note": "This endpoint will process webhooks that include a 'changed_files' array. "
            "For GitHub pull_request webhooks that don't include file lists, fetch PR files via GitHub API and call /predict-impact.",
        }
        return hint
