from __future__ import annotations

import json
import time
import uuid
from typing import Any, Final

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

PORT: Final = 8080
ECHO_PREFIX: Final = "echoed:"
TOOL_ARGUMENT: Final = "hi"
EMBEDDING: Final = [0.1, 0.2, 0.3]
USAGE: Final = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
DEFAULT_PROTOCOL: Final = "2025-06-18"
ECHO_TOOL: Final = {
    "name": "echo",
    "description": "Returns its text with a prefix.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}

app: Final = FastAPI()
seen: Final[list[dict[str, str]]] = []
stored: Final[dict[str, dict[str, Any]]] = {}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    body: Final = await request.json()
    model: Final = str(body["model"])
    messages: Final = body.get("messages") or []
    seen.append({"model": model, "prompt": _first_user_text(messages)})
    tool_names: Final = [
        entry["function"]["name"]
        for entry in body.get("tools") or []
        if entry.get("type") == "function"
    ]
    answered: Final = any(message.get("role") == "tool" for message in messages)
    if tool_names and not answered:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": tool_names[0],
                        "arguments": json.dumps({"text": TOOL_ARGUMENT}),
                    },
                }
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": f"answered by {model}"}
        finish_reason = "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": USAGE,
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    body: Final = await request.json()
    return {
        "object": "list",
        "model": str(body["model"]),
        "data": [{"object": "embedding", "index": 0, "embedding": EMBEDDING}],
        "usage": {"prompt_tokens": 10, "total_tokens": 10},
    }


@app.post("/v1/responses")
async def create_response(request: Request) -> dict[str, Any]:
    body: Final = await request.json()
    response: Final = _response(body, "response", "answered by")
    stored[response["id"]] = response
    return response


@app.post("/v1/responses/compact")
async def compact_response(request: Request) -> dict[str, Any]:
    return _response(await request.json(), "response.compaction", "compacted by")


@app.get("/v1/responses/{response_id}")
async def read_response(response_id: str) -> Response:
    found: Final = stored.get(response_id)
    if found is None:
        return JSONResponse({"error": {"message": "no such response"}}, 404)
    return JSONResponse(found)


@app.get("/v1/files")
async def list_files() -> dict[str, Any]:
    return {"object": "list", "data": [], "has_more": False}


@app.get("/requests")
async def requests(prompt: str = "") -> list[str]:
    return [entry["model"] for entry in seen if prompt and prompt in entry["prompt"]]


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    message: Final = await request.json()
    if "id" not in message:
        return Response(status_code=202)
    return JSONResponse(
        {"jsonrpc": "2.0", "id": message["id"], **_answer(message)},
        headers={"mcp-protocol-version": DEFAULT_PROTOCOL},
    )


@app.get("/mcp")
async def mcp_stream() -> Response:
    return Response(status_code=405)


@app.delete("/mcp")
async def mcp_session_end() -> Response:
    return Response(status_code=200)


def _answer(message: dict[str, Any]) -> dict[str, Any]:
    params: Final = message.get("params") or {}
    match message.get("method"):
        case "initialize":
            return {
                "result": {
                    "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "lab-tools", "version": "1"},
                }
            }
        case "ping":
            return {"result": {}}
        case "tools/list":
            return {"result": {"tools": [ECHO_TOOL]}}
        case "tools/call":
            arguments = params.get("arguments") or {}
            text = f"{ECHO_PREFIX}{arguments.get('text', '')}"
            return {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }
            }
        case method:
            return {"error": {"code": -32601, "message": f"unknown method {method}"}}


def _response(body: dict[str, Any], kind: str, verb: str) -> dict[str, Any]:
    model: Final = str(body["model"])
    seen.append({"model": model, "prompt": _input_text(body.get("input"))})
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": kind,
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"{verb} {model}",
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    }


def _input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    for item in value:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
    return ""


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            return content
    return ""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
