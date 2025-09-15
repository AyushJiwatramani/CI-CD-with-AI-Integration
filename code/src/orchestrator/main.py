# code/src/orchestrator/main.py
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
import os
import logging

from analyzer.static import get_impacted_modules, map_files_to_modules

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
    """
    Accept either:
    - JSON body with "changed_files": [ ... ]
    - A GitHub-style webhook body may be posted to /webhook (here we only accept explicit changed_files)
    """
    repo_root = req.repo_root or DEFAULT_REPO_ROOT
    group_depth = req.group_depth if req.group_depth is not None else 2
    max_hops = req.max_hops if req.max_hops is not None else 1

    if not req.changed_files or not isinstance(req.changed_files, list) or len(req.changed_files) == 0:
        raise HTTPException(status_code=400, detail="Please provide non-empty 'changed_files' list in body.")

    # defensive: clean inputs
    changed_files = [str(x).strip() for x in req.changed_files if isinstance(x, str) and x.strip()]

    if not changed_files:
        raise HTTPException(status_code=400, detail="No valid file paths found in 'changed_files' list.")

    # compute impacted modules via analyzer
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

    # recommended jobs -> simple "unit:<module>" naming
    recommended_jobs = [f"unit:{p['component']}" for p in preds]

    # Build response
    response = {
        "predictions": preds,
        "recommended_jobs": recommended_jobs,
    }
    return response


@app.post("/webhook")
async def webhook_handler(request: Request, x_github_event: Optional[str] = Header(None)):
    """
    Generic webhook receiver.
    If the webhook body contains a 'changed_files' list (e.g., from a proxy that resolves files),
    we will process it. Otherwise we return 202 Accepted and instructions.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = None

    # If user posts a payload with changed_files, delegate to predict-impact logic
    if payload and isinstance(payload, dict) and "changed_files" in payload:
        try:
            body = PredictRequest(**{"changed_files": payload["changed_files"]})
            return await predict_impact(body)
        except Exception as e:
            logger.exception("Webhook -> predict error: %s", e)
            raise HTTPException(status_code=400, detail="Invalid webhook payload for changed_files.")
    else:
        # We cannot fetch changed files from a raw GitHub webhook without calling GH APIs.
        # So we accept the event and tell the user how to follow-up:
        hint = {
            "message": "Received webhook.",
            "event": x_github_event,
            "note": "This endpoint will process webhooks that include a 'changed_files' array. "
            "For GitHub pull_request webhooks that don't include file lists, you must either call /predict-impact with changed_files, "
            "or implement a GitHub App / script that fetches PR file list and posts it here.",
        }
        return hint
