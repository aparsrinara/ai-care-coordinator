"""Smoke test: confirm we can reach the Bedrock model configured in .env."""
import os

import boto3
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
if not token:
    raise SystemExit("AWS_BEARER_TOKEN_BEDROCK is empty - paste your Bedrock API key into backend/.env")

model_id = os.environ["BEDROCK_MODEL_ID"]
client = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])
resp = client.converse(
    modelId=model_id,
    messages=[{"role": "user", "content": [{"text": "Reply with exactly: Bedrock OK"}]}],
    inferenceConfig={"maxTokens": 200},
)
text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
print(f"{model_id} -> {text.strip()}")
