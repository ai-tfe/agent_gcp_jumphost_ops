"""Agent Runtime HTTP contract for the containerized GCP Jump Host Automation Agent."""

import inspect
import json
import logging
import os
from typing import Any

import uvicorn
import vertexai
from fastapi import FastAPI, Request, encoders, responses
from google.adk import runners as adk_runners
from vertexai import agent_engines

from gcp_jumphost_ops_agent.agent import root_agent

# Sanitize numeric GOOGLE_CLOUD_PROJECT injected by Agent Engine to avoid CRM gRPC call
_raw_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
if not _raw_project or _raw_project.isdigit():
    os.environ["GOOGLE_CLOUD_PROJECT"] = "svcprjprd"
if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = "us-east4"

CARD_PATH = os.environ.get("AGENT_CARD_PATH", "/app/gcp_jumphost_ops_agent/agent.json")
PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ["GOOGLE_CLOUD_LOCATION"]


def enable_adk_runner_compatibility() -> None:
    """Ignore AdkApp's newer kwarg when the installed Runner lacks it."""
    original_init = adk_runners.Runner.__init__
    if "auto_create_session" in inspect.signature(original_init).parameters:
        return

    def compatible_init(self, *args, **kwargs) -> None:
        kwargs.pop("auto_create_session", None)
        original_init(self, *args, **kwargs)

    adk_runners.Runner.__init__ = compatible_init


enable_adk_runner_compatibility()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logging.info(
    "egress proxy http=%s https=%s no_proxy=%s",
    os.getenv("HTTP_PROXY") or os.getenv("http_proxy"),
    os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"),
    os.getenv("NO_PROXY") or os.getenv("no_proxy"),
)
vertexai.init(project=PROJECT_ID, location=LOCATION)
adk_app = agent_engines.AdkApp(agent=root_agent)
app = FastAPI(title="GCP Jump Host Automation Agent")


def encode_chunk(chunk: Any) -> str | None:
    try:
        return json.dumps(encoders.jsonable_encoder(chunk)) + "\n"
    except Exception:
        logging.exception("Failed to encode chunk")
        return None


async def json_lines(output: Any):
    """Support both sync generators (stream_query) and async generators (async_stream_query)."""
    if hasattr(output, "__aiter__"):
        async for chunk in output:
            encoded = encode_chunk(chunk)
            if encoded is None:
                break
            yield encoded
    else:
        for chunk in output:
            encoded = encode_chunk(chunk)
            if encoded is None:
                break
            yield encoded


async def invoke(method, payload: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(**payload)
    return method(**payload)


def agent_card(url: str) -> dict[str, Any]:
    """Load the agent-owned A2A v1.0 card and apply this deployment's URL."""
    with open(CARD_PATH, encoding="utf-8") as card_file:
        card = json.load(card_file)
    if url:
        if "supportedInterfaces" in card and card["supportedInterfaces"]:
            card["supportedInterfaces"][0]["url"] = url
        elif "url" in card:
            card["url"] = url
    return card


def card_url(request: Request | None = None) -> str:
    configured = os.getenv("AGENT_URL", "").rstrip("/")
    if configured:
        return configured
    if request is None:
        return ""
    return str(request.base_url).rstrip("/")


def card_response(card: dict[str, Any]) -> responses.JSONResponse:
    return responses.JSONResponse(
        content=card,
        media_type="application/a2a+json",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
def well_known_agent_card(request: Request) -> responses.JSONResponse:
    return card_response(agent_card(card_url(request)))


@app.get("/.well-known/agent.json")
def legacy_agent_card(request: Request) -> responses.JSONResponse:
    return card_response(agent_card(card_url(request)))


@app.post("/api/reasoning_engine")
async def query(request: Request) -> responses.JSONResponse:
    body = await request.json()
    if body.get("class_method") == "get_agent_card":
        return responses.JSONResponse(
            content=encoders.jsonable_encoder(
                {"output": agent_card(card_url(request))}
            )
        )
    method = getattr(adk_app, body["class_method"])
    output = await invoke(method, body.get("input") or {})
    return responses.JSONResponse(
        content=encoders.jsonable_encoder({"output": output})
    )


@app.post("/api/stream_reasoning_engine")
async def stream_query(request: Request) -> responses.StreamingResponse:
    body = await request.json()
    method = getattr(adk_app, body["class_method"])
    output = await invoke(method, body.get("input") or {})
    return responses.StreamingResponse(
        content=json_lines(output),
        media_type="application/json",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
