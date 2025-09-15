# minimal FastAPI orchestrator
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict
import uvicorn
import os
from analyzer.static import map_files_to_modules

app = FastAPI()

class PredictRequest(BaseModel):
    pr_number: int = None
    changed_files: List[str]

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/predict-impact")
def predict(req: PredictRequest):
    # simple heuristic: map changed files -> modules
    modules = map_files_to_modules(req.changed_files)
    # recommended jobs are unit test names for each module
    recommended_jobs = [f"unit:{m}" for m in modules]
    predictions = [{"component": m, "confidence": 0.9, "reason": "direct-change"} for m in modules]
    return {"predictions": predictions, "recommended_jobs": recommended_jobs}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
