"""Local OpenAI-compatible HTTP stand-in used as an external test dependency."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProviderState:
    def __init__(self):
        self.requests = []
        self.incomplete_editorial_once = False
        self._lock = threading.Lock()

    def append(self, request):
        with self._lock:
            self.requests.append(request)

    def snapshot(self):
        with self._lock:
            return list(self.requests)

    def clear(self):
        with self._lock:
            self.requests.clear()

    def take_editorial_failure(self):
        with self._lock:
            value = self.incomplete_editorial_once
            self.incomplete_editorial_once = False
            return value


class OpenAIStandin:
    """Serve deterministic schema-shaped JSON at `/v1/chat/completions`."""

    def __init__(self):
        self.state = ProviderState()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner_self):
                length = int(inner_self.headers.get("Content-Length", "0"))
                request = json.loads(inner_self.rfile.read(length))
                self.state.append(request)
                name, schema = _response_schema(request)
                prompts = _prompt_text(request)
                payload = _response_payload(name, schema, prompts)
                if name == "EditorialTranslation" and self.state.take_editorial_failure():
                    payload = _make_editorial_incomplete(payload)
                content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                response = {
                    "id": "chatcmpl-acceptance",
                    "object": "chat.completion",
                    "created": 1_798_000_000,
                    "model": request.get("model", "gpt-6-luna"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 96,
                        "completion_tokens": 48,
                        "total_tokens": 144,
                    },
                }
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                inner_self.send_response(200)
                inner_self.send_header("Content-Type", "application/json")
                inner_self.send_header("Content-Length", str(len(body)))
                inner_self.end_headers()
                inner_self.wfile.write(body)

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _response_schema(request):
    response_format = request.get("response_format", {})
    json_schema = response_format.get("json_schema", {})
    schema = json_schema.get("schema", {})
    return json_schema.get("name", ""), schema


def _prompt_text(request):
    chunks = []
    for message in request.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return "\n".join(chunks)


def _candidate_numbers(prompt):
    candidates = re.findall(r"(?m)^\s*(\d+)\s*[.)]\s+", prompt)
    if not candidates:
        candidates = re.findall(r"(?i)(?:article|candidate|number)\s*[:#]?\s*(\d+)", prompt)
    values = []
    for value in candidates:
        if value not in values:
            values.append(value)
    return values or ["1"]


def _summary_ids(prompt):
    values = re.findall(r'"summary_id"\s*:\s*(\d+)', prompt)
    if not values:
        values = re.findall(r"(?i)summary\s+id\s*[:#]?\s*(\d+)", prompt)
    return list(dict.fromkeys(values))


def _response_payload(name, schema, prompt):
    if name == "StoryTranslation":
        return {
            "title_local": "Türkçe başlık wave2",
            "summary": "Türkçe özet wave2 aranabilir metin",
            "why_it_matters": "Türkçe önemi wave2",
        }
    if name == "EditorialTranslation":
        ids = _summary_ids(prompt) or _candidate_numbers(prompt)
        return {
            "editor_note": "Türkçe editör notu wave2",
            "reasons": [
                {"summary_id": int(summary_id), "reason": f"Türkçe gerekçe {summary_id}"}
                for summary_id in ids
            ],
        }
    return _build_value(schema, "", name, prompt, 0, schema)


def _make_editorial_incomplete(payload):
    broken = dict(payload)
    broken["reasons"] = []
    return broken


def _build_value(schema, field, schema_name, prompt, index, root_schema):
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return None
        target = root_schema
        for component in ref[2:].split("/"):
            target = target[component.replace("~1", "/").replace("~0", "~")]
        schema = target
    if "anyOf" in schema:
        choices = [option for option in schema["anyOf"] if option.get("type") != "null"]
        schema = choices[0] if choices else schema["anyOf"][0]
    if "oneOf" in schema:
        schema = schema["oneOf"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        return {
            key: _build_value(value, key, schema_name, prompt, index, root_schema)
            for key, value in properties.items()
        }
    if kind == "array":
        item_schema = schema.get("items", {})
        if field in {"reasons", "reason_translations"}:
            return _response_payload("EditorialTranslation", {}, prompt)["reasons"]
        if field in {"tags", "topics", "tag_names"}:
            return ["acceptance-wave-topic"]
        if field in {"picks", "items", "stories", "selected"}:
            numbers = _candidate_numbers(prompt)
            return [
                _build_value(item_schema, field, schema_name, prompt, item_index, root_schema)
                for item_index, _ in enumerate(numbers[:8])
            ]
        return []
    if schema.get("enum"):
        return schema["enum"][0]
    if kind == "boolean":
        return field in {"relevant", "is_relevant", "keep"}
    if kind in {"integer", "number"}:
        candidates = _candidate_numbers(prompt)
        if field in {"number", "candidate_number", "article_number", "summary_id"}:
            return int(candidates[min(index, len(candidates) - 1)])
        if field in {"importance", "score", "tier"}:
            return 3 if kind == "integer" else 0.82
        return 1 if kind == "integer" else 0.82
    if kind == "string":
        lowered = field.casefold()
        if "title" in lowered or "headline" in lowered:
            return f"Acceptance English headline {index + 1}"
        if "summary" in lowered or "abstract" in lowered:
            return f"Canonical English summary wave2 {index + 1} includes figure 42."
        if "why" in lowered or "reason" in lowered:
            return f"Canonical English reason wave2 {index + 1}."
        if "fact" in lowered:
            return "42 units"
        if "tag" in lowered or "topic" in lowered:
            return "acceptance-wave-topic"
        if "kind" in lowered:
            return "model_release"
        if "language" in lowered:
            return "en"
        return f"acceptance {field or schema_name}"
    return None
