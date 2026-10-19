#!/usr/bin/env python3
"""Local stand-in for OpenAI and Anthropic, for trying the demo with no API keys
or no network. It answers chat completions and messages requests with a short canned
reply and realistic token usage, so agentgateway prices, logs and budgets them as usual.

    python3 scripts/mock-llm.py            # listens on 127.0.0.1:3091
"""
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_LLM_PORT", "3091"))
REPLY = "Mock reply from the local stand-in. No API key was used."

def tokens(text):            # rough but stable: ~4 characters per token
    return max(1, len(text) // 4)

def prompt_text(body):
    parts = []
    if isinstance(body.get("system"), str):
        parts.append(body["system"])
    for m in body.get("messages", []):
        c = m.get("content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [b.get("text", "") for b in c if isinstance(b, dict)]
    return "\n".join(parts)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for name, data in events:
            line = (f"event: {name}\n" if name else "") + f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n"
            self.wfile.write(line.encode())
            self.wfile.flush()
            time.sleep(0.02)
        self.close_connection = True

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "mock", "object": "model"}]})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "mock")
        cap = int(body.get("max_tokens") or body.get("max_completion_tokens") or 256)
        in_tokens = tokens(prompt_text(body))
        # Long prompts are the budget probes. Report a full completion so trying the
        # demo without keys trips a $0.02 cap on about the same request a chatty model does.
        # Short questions stay short, so a one-line ask does not look like a 600-token bill.
        if in_tokens > 400:
            out_tokens = min(cap, 300)
        else:
            out_tokens = min(cap, 40 + in_tokens // 20)
        stream = bool(body.get("stream"))
        if self.path.rstrip("/").endswith("/messages"):          # Anthropic Messages API
            usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}
            if not stream:
                return self._send(200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                                        "content": [{"type": "text", "text": REPLY}], "stop_reason": "end_turn",
                                        "stop_sequence": None, "usage": usage})
            return self._sse([
                ("message_start", {"type": "message_start", "message": {"id": "msg_mock", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None, "usage": {"input_tokens": in_tokens, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": out_tokens}}),
                ("message_stop", {"type": "message_stop"}),
            ])
        if self.path.rstrip("/").endswith("/chat/completions"):  # OpenAI Chat Completions
            usage = {"prompt_tokens": in_tokens, "completion_tokens": out_tokens, "total_tokens": in_tokens + out_tokens}
            if not stream:
                return self._send(200, {"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
                                        "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": REPLY}}], "usage": usage})
            base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
            return self._sse([
                (None, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY}, "finish_reason": None}]}),
                (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                (None, {**base, "choices": [], "usage": usage}),
                (None, "[DONE]"),
            ])
        self._send(404, {"error": {"message": f"mock does not implement {self.path}"}})

    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_LLM_VERBOSE"):
            sys.stderr.write("mock-llm: " + (fmt % args) + "\n")

if __name__ == "__main__":
    print(f"mock-llm listening on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
