"""Bedrock calls. Every function returns None on failure so callers can fall back."""
import json
import logging
import os
import re
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "openai.gpt-oss-120b-1:0")
_client = boto3.client(
    "bedrock-runtime",
    region_name=os.getenv("AWS_REGION", "us-east-1"),
    config=Config(read_timeout=30, connect_timeout=5, retries={"mode": "standard", "max_attempts": 1}),
)
# botocore's read_timeout is per socket read, so a slow trickle can run for minutes.
# Calls run on this pool so the caller can give up at a hard deadline and fall back.
_pool = ThreadPoolExecutor(max_workers=16)


# Running totals, exposed at /api/metrics.
STATS = {"calls": 0, "failures": 0, "inputTokens": 0, "outputTokens": 0, "seconds": 0.0, "byPurpose": {}}
_stats_lock = threading.Lock()


def _record(purpose: str, usage: dict, seconds: float):
    with _stats_lock:
        STATS["calls"] += 1
        STATS["inputTokens"] += usage.get("inputTokens", 0)
        STATS["outputTokens"] += usage.get("outputTokens", 0)
        STATS["seconds"] += seconds
        p = STATS["byPurpose"].setdefault(purpose, {"calls": 0, "inputTokens": 0, "outputTokens": 0})
        p["calls"] += 1
        p["inputTokens"] += usage.get("inputTokens", 0)
        p["outputTokens"] += usage.get("outputTokens", 0)


def complete(system: str, messages: list[dict], max_tokens: int = 1200, effort: str | None = None,
             purpose: str = "other", deadline: float = 60) -> str | None:
    """messages: [{"role": "user"|"assistant", "text": str}, ...]
    effort: gpt-oss reasoning effort ("low" answers chat in ~1s instead of ~4-8s)."""
    extra = {"additionalModelRequestFields": {"reasoning_effort": effort}} if effort else {}
    started = time.time()
    future = _pool.submit(
        _client.converse,
        modelId=MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in messages],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
        **extra,
    )
    try:
        resp = future.result(timeout=deadline)
    except FutureTimeout:
        log.warning("Bedrock %s call passed the %ss deadline; falling back", purpose, deadline)
        with _stats_lock:
            STATS["failures"] += 1
            STATS["timeouts"] = STATS.get("timeouts", 0) + 1
        return None
    except Exception:
        log.exception("Bedrock call failed")
        with _stats_lock:
            STATS["failures"] += 1
        return None
    _record(purpose, resp.get("usage", {}), time.time() - started)
    # gpt-oss also returns reasoningContent blocks; keep only the answer text.
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
    return text or None


SUMMARY_SYSTEM = """You turn a doctor's visit note into a plain-English summary for the patient.
Return only JSON, no code fences: {"summary": "...", "nextSteps": [{"id": "...", "title": "..."}]}
- summary: 2 sentences, under 50 words, warm and clear, 6th-grade reading level. Say what the doctor wants done. No diagnoses the note doesn't state, no medical advice, no jargon or abbreviations.
- nextSteps: one per order in the plan, using these ids when they apply: labs, refill, derm, pt. title is 2-4 words."""


def summarize_visit(note: str) -> dict | None:
    text = complete(SUMMARY_SYSTEM, [{"role": "user", "text": note}], max_tokens=1500, purpose="visit_summary")
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not data or not isinstance(data.get("summary"), str):
        log.warning("Unexpected visit summary output: %s", text[:300])
        return None
    return data


RESULTS_SYSTEM = """You explain lab results to a patient in plain English. You are not a doctor.
Return 2 to 3 short sentences, under 60 words, 6th-grade reading level, no jargon.
- Say which results are in range and which aren't, and the trend if a prior value is given.
- Repeat the doctor's comment and plan faithfully. Add no advice, diagnosis or reassurance beyond what the doctor wrote.
- End with: questions go to the doctor who ordered the test.
Plain text only."""


def explain_results(results_text: str) -> str | None:
    return complete(RESULTS_SYSTEM, [{"role": "user", "text": results_text}], max_tokens=1200, purpose="lab_explanation")
