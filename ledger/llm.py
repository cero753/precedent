"""Chat with tool calling against your LLM API (any provider with an OpenAI-compatible chat completions endpoint).

Configure in .env (git-ignored):
  LLM_API_KEY=...        your key
  LLM_MODEL=...          any tool-calling model id offered by your provider
  LLM_BASE_URL=...       your provider's API base URL (the part before /chat/completions)

Messages and tools use the OpenAI shape:
  messages: [{"role": "user"|"assistant"|"tool", ...}]
  tools:    [{"name", "description", "parameters"}]
"""
import json
import os
import urllib.request
from pathlib import Path


def load_env(path=None):
    """Minimal .env loader (no dependency). Existing environment variables win."""
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def provider():
    """'llm' when key, model and base URL are configured, else None (offline)."""
    load_env()
    return "llm" if all(os.environ.get(k) for k in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")) else None


def model_name():
    load_env()
    return os.environ.get("LLM_MODEL", "not configured")


def chat(system, messages, tools=None, force_tool=None, max_tokens=1200):
    """Returns {"text": str, "tool_calls": [{"id", "name", "args"}], "usage": {...}}."""
    if not provider():
        raise RuntimeError("No LLM configured: set LLM_API_KEY, LLM_MODEL and LLM_BASE_URL in .env")
    body = {"model": model_name(), "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + messages}
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
        if force_tool:
            body["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
    url = os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {os.environ['LLM_API_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(f"LLM API error: {data['error']}")
    msg = data["choices"][0]["message"]
    calls = []
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_invalid_json": c["function"].get("arguments")}
        calls.append({"id": c["id"], "name": c["function"]["name"], "args": args})
    return {"text": msg.get("content") or "", "tool_calls": calls, "usage": data.get("usage", {})}


def assistant_message(resp):
    """Convert a chat() response back into an assistant message for the history."""
    m = {"role": "assistant", "content": resp["text"] or None}
    if resp["tool_calls"]:
        m["tool_calls"] = [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in resp["tool_calls"]]
    return m
