"""Bedrock calls. Every function returns None on failure so callers can fall back."""
import json
import logging
import os
import re

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "openai.gpt-oss-120b-1:0")
_client = boto3.client(
    "bedrock-runtime",
    region_name=os.getenv("AWS_REGION", "us-east-1"),
    config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1}),
)


def complete(system: str, messages: list[dict], max_tokens: int = 1200, effort: str | None = None) -> str | None:
    """messages: [{"role": "user"|"assistant", "text": str}, ...]
    effort: gpt-oss reasoning effort ("low" answers chat in ~1s instead of ~4-8s)."""
    extra = {"additionalModelRequestFields": {"reasoning_effort": effort}} if effort else {}
    try:
        resp = _client.converse(
            modelId=MODEL_ID,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in messages],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
            **extra,
        )
    except Exception:
        log.exception("Bedrock call failed")
        return None
    # gpt-oss also returns reasoningContent blocks; keep only the answer text.
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
    return text or None


SUMMARY_SYSTEM = """You turn a doctor's visit note into a plain-English summary for the patient.
Return only JSON, no code fences: {"summary": "...", "nextSteps": [{"id": "...", "title": "..."}]}
- summary: 2 sentences, under 50 words, warm and clear, 6th-grade reading level. Say what the doctor wants done. No diagnoses the note doesn't state, no medical advice, no jargon or abbreviations.
- nextSteps: one per order in the plan, using these ids when they apply: labs, refill, derm, pt. title is 2-4 words."""


def summarize_visit(note: str) -> dict | None:
    text = complete(SUMMARY_SYSTEM, [{"role": "user", "text": note}], max_tokens=1500)
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
