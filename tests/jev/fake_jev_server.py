"""A deterministic stand-in for api.typesafe.ai used by the protocol tests.

    python tests/fake_jev_server.py 8766      # serve on 127.0.0.1:8766

POST /v1/systemone answers every question from a hash of (state, question content),
never the question id, because the real model never sees ids either:
noul -> a probability, choice -> a full distribution, score -> a distribution over
levels. A state whose text contains "__status_429__" (any three-digit status) makes the
server respond with that HTTP status once (429 carries Retry-After: 1) so retry
paths can be tested.
GET /v1/models lists one fake model. Requests without a Bearer token get 401.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "jev-fake-1.0"


def _unit(*parts):
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 2 ** 32


def _distribution(keys, seed):
    weights = [_unit(seed, k) ** 3 + 0.01 for k in keys]
    total = sum(weights)
    return {k: round(w / total, 4) for k, w in zip(keys, weights)}


def _confidence(probabilities):
    n = len(probabilities)
    peak = max(probabilities.values())
    return round(max(0.0, min(1.0, (n * peak - 1) / (n - 1))), 4) if n > 1 else 1.0


def _question_seed(question):
    return json.dumps({"type": question.get("type"), "instructions": question.get("instructions"),
                       "criteria": question.get("criteria")}, sort_keys=True)


def answer(state_json, qid, question):
    qtype = question.get("type")
    seed = _question_seed(question)
    if qtype == "noul":
        return {"type": "noul", "noul": round(_unit(state_json, seed), 4)}
    if qtype == "choice":
        keys = list(question.get("criteria", {}).keys())
        probabilities = _distribution(keys, state_json + seed)
        best = max(probabilities, key=probabilities.get)
        return {"type": "choice", "choice": best, "probabilities": probabilities, "confidence": _confidence(probabilities)}
    if qtype == "score":
        levels = question.get("criteria", [])
        keys = [str(i) for i in range(len(levels))]
        probabilities = _distribution(keys, state_json + seed)
        score = sum(int(k) * p for k, p in probabilities.items())
        legend = {k: levels[int(k)] for k in keys}
        return {"type": "score", "score": round(score, 4), "legend": legend, "probabilities": probabilities,
                "confidence": _confidence(probabilities)}
    return {"type": "error", "error": "unknown type %r" % qtype}


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-jev/1.0"
    once_failed = set()
    log = []
    questions_seen = 0

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and len(auth) > 7

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": "missing bearer token"})
        if self.path.endswith("/models"):
            return self._send(200, {"models": [{"name": "jev-latest", "description": "fake", "release_date": "2026-01-01"},
                                               {"name": MODEL, "description": "fake versioned", "release_date": "2026-01-01"}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if not self._authorized():
            return self._send(401, {"error": "missing bearer token"})
        if not self.path.endswith("/systemone"):
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(422, {"detail": [{"loc": ["body"], "msg": "invalid json"}]})
        Handler.log.append(body)
        for field in ("state", "model", "questions"):
            if field not in body:
                return self._send(422, {"detail": [{"loc": ["body", field], "msg": "field required"}]})
        state = body["state"]
        state_json = json.dumps(state, sort_keys=True)
        marker = re.search(r"__status_(\d{3})__", state_json)
        if marker and state_json not in Handler.once_failed:
            status = int(marker.group(1))
            Handler.once_failed.add(state_json)
            return self._send(status, {"error": "injected %d" % status}, {"Retry-After": "1"} if status == 429 else None)
        answers = {qid: answer(state_json, qid, q) for qid, q in body["questions"].items()}
        Handler.questions_seen += len(answers)
        tokens = max(20, len(raw) // 4)
        self._send(200, {"model": MODEL, "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 8 * len(answers)}})


def serve(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


if __name__ == "__main__":
    server, port = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8766)
    print("fake jev on http://127.0.0.1:%d/v1" % port, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
