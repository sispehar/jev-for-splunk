"""HTTP client for TypeSafe's System One endpoint, standard library only.

Retries 429/529/5xx and network errors with exponential backoff, honours
Retry-After, shares a cooldown across threads after a 429, and stops the
whole search fast after a 401.
"""
from __future__ import annotations

import email.utils
import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import USER_AGENT
from .ratelimit import TokenBucket

RETRY_STATUSES = frozenset([429, 500, 502, 503, 504, 529])
MAX_BACKOFF = 60.0
MAX_RETRY_AFTER = 120.0


class JevError(Exception):
    """A failed judgment. `code` is the short token written to jev_error."""

    def __init__(self, code, message="", retryable=False, status=None):
        super(JevError, self).__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.status = status


class JevResult(object):
    __slots__ = ("answers", "model", "usage", "latency_ms", "attempts")

    def __init__(self, answers, model, usage, latency_ms, attempts):
        self.answers = answers
        self.model = model
        self.usage = usage or {}
        self.latency_ms = latency_ms
        self.attempts = attempts

    @property
    def input_tokens(self):
        return int(self.usage.get("input_tokens") or 0)

    @property
    def output_tokens(self):
        return int(self.usage.get("output_tokens") or 0)


def parse_retry_after(value, now=None):
    if not value:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (now if now is not None else time.time())
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def _validation_field(body):
    """Best effort: pull the offending field path out of a 422 body."""
    try:
        data = json.loads(body)
    except ValueError:
        return "unknown"
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, list) and detail:
        loc = detail[0].get("loc") if isinstance(detail[0], dict) else None
        if isinstance(loc, list) and loc:
            return ".".join(str(part) for part in loc if part != "body") or "unknown"
        return str(detail[0])[:60]
    if isinstance(detail, str):
        return detail[:60]
    if isinstance(data, dict):
        for key in ("field", "param", "error", "message"):
            if key in data:
                return str(data[key])[:60]
    return "unknown"


def build_opener(proxy_url="", ca_bundle=""):
    context = ssl.create_default_context(cafile=ca_bundle or None)
    handlers = [urllib.request.HTTPSHandler(context=context)]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class JevClient(object):
    def __init__(self, api_key, endpoint="https://api.typesafe.ai/v1", model="jev-latest", timeout=30.0,
                 max_retries=5, requests_per_second=15.0, burst=8, proxy_url="", ca_bundle="",
                 urlopen=None, sleep=time.sleep, clock=time.monotonic, logger=None):
        if not api_key:
            raise JevError("api_key_missing", "no TypeSafe API key configured")
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self._sleep = sleep
        self._clock = clock
        self._log = logger
        self._opener = None if urlopen else build_opener(proxy_url, ca_bundle)
        self._urlopen = urlopen or self._opener.open
        self._bucket = TokenBucket(requests_per_second, burst, clock=clock, sleep=sleep)
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self.auth_dead = False
        self.requests_made = 0
        self.retries = 0

    # -- public -----------------------------------------------------------------
    def judge(self, state, questions, model=None):
        body = {"state": state, "model": model or self.model, "questions": questions}
        data, attempts, latency_ms = self._request("POST", "/systemone", body)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise JevError("bad_response", "response has no answers object")
        return JevResult(answers, data.get("model", ""), data.get("usage") or {}, latency_ms, attempts)

    def list_models(self):
        data, _, _ = self._request("GET", "/models", None)
        models = data.get("models") if isinstance(data, dict) else data
        return models or []

    # -- internals --------------------------------------------------------------
    def _wait_cooldown(self):
        while True:
            with self._lock:
                remaining = self._cooldown_until - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(remaining, 1.0))

    def _set_cooldown(self, seconds):
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    def _backoff(self, attempt, retry_after=None):
        if retry_after is not None:
            return retry_after + random.uniform(0, 0.25)
        return min(MAX_BACKOFF, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.25)

    def _request(self, method, path, body):
        url = self.endpoint + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        attempt = 0
        while True:
            if self.auth_dead:
                raise JevError("auth_failed", "the TypeSafe API rejected the key earlier in this search", status=401)
            self._wait_cooldown()
            self._bucket.acquire()
            attempt += 1
            request = urllib.request.Request(url, data=payload, method=method)
            request.add_header("Authorization", "Bearer " + self.api_key)
            request.add_header("Accept", "application/json")
            request.add_header("User-Agent", USER_AGENT)
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            started = self._clock()
            try:
                response = self._urlopen(request, timeout=self.timeout)
                raw = response.read()
                latency_ms = (self._clock() - started) * 1000.0
                self.requests_made += 1
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    raise JevError("bad_response", "response body is not JSON")
                return data, attempt, latency_ms
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    text = exc.read().decode("utf-8", "replace")
                except Exception:
                    text = ""
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                if status in (401, 403):
                    self.auth_dead = True
                    raise JevError("auth_failed", "HTTP %d from TypeSafe: check the API key" % status, status=status)
                if status == 422:
                    raise JevError("validation:" + _validation_field(text), "HTTP 422: %s" % text[:300], status=status)
                if status in RETRY_STATUSES:
                    if attempt > self.max_retries:
                        code = "rate_limited" if status in (429, 529) else "http_%d" % status
                        raise JevError(code, "HTTP %d after %d attempts" % (status, attempt), retryable=True, status=status)
                    delay = self._backoff(attempt, retry_after)
                    if status in (429, 529):
                        self._set_cooldown(delay)
                    self.retries += 1
                    self._debug("retry %d after HTTP %d in %.1fs", attempt, status, delay)
                    self._sleep(delay)
                    continue
                raise JevError("http_%d" % status, "HTTP %d: %s" % (status, text[:300]), status=status)
            except JevError:
                raise
            except (socket.timeout, urllib.error.URLError, OSError, ValueError) as exc:
                reason = _network_reason(exc)
                if reason == "tls_verify":
                    raise JevError("network:tls_verify",
                                   "TLS verification failed; set ca_bundle in jev.conf [api] (%s)" % exc)
                if attempt > self.max_retries:
                    raise JevError("network:" + reason, "%s after %d attempts: %s" % (reason, attempt, exc), retryable=True)
                delay = self._backoff(attempt)
                self.retries += 1
                self._debug("retry %d after %s in %.1fs", attempt, reason, delay)
                self._sleep(delay)

    def _debug(self, fmt, *args):
        if self._log is not None:
            self._log.debug(fmt, *args)


def _network_reason(exc):
    text = str(exc).lower()
    if isinstance(exc, socket.timeout) or "timed out" in text or "timeout" in text:
        return "timeout"
    if "certificate" in text or "ssl" in text:
        return "tls_verify" if "verify" in text else "tls"
    if "name or service not known" in text or "nodename" in text or "getaddrinfo" in text:
        return "dns"
    if "refused" in text:
        return "refused"
    return "error"
