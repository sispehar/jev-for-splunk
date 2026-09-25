"""Chunk orchestration shared by the Splunk command, the CLI and the tests.

Per chunk of records:
  1. plan   - build each record's state and questions; one cache key per (state, question)
  2. memo   - answers already seen in this search are reused
  3. cache  - the persistent backend (KV store, sqlite) is asked for the rest
  4. send   - what is still missing is grouped by state: one request per state,
              carrying only the missing questions (they run in parallel inside Jev)
  5. store  - successful answers are written back to the backend
  6. fill   - every record gets every schema field

Cache modes: kv (memo + backend read/write), refresh (memo, backend write only),
memo (memo only), off (no reuse at all: duplicates each get a request).
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .answers import (blank_fields, fill_answer, fill_meta, fill_primitive_meta, output_schema,
                      primitive_schema)
from .battery import wire_questions
from .cache import CacheReadOnly, CacheUnavailable, cache_key, doc_answer, make_doc
from .client import JevError
from .state import build_state, canonical_json, state_hash


class Stats(object):
    def __init__(self):
        self.records = 0
        self.evaluated = 0        # records whose questions were all answered (fresh or reused)
        self.requests = 0         # API calls actually made
        self.cached = 0           # records answered entirely without a new request
        self.fresh = 0            # answers judged in this search
        self.kv_hits = 0          # answers read from the persistent cache
        self.memo_hits = 0        # answers reused within this search
        self.errors = OrderedDict()
        self.skipped_no_state = 0
        self.budget_exceeded = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.est_tokens = 0.0     # dryrun estimate for the questions that would be sent
        self.kv_get_ms = 0.0
        self.kv_put_ms = 0.0
        self.kv_saved = 0
        self.latencies = []

    def note_error(self, code):
        self.errors[code] = self.errors.get(code, 0) + 1

    def p50_latency(self):
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        return ordered[len(ordered) // 2]

    def summary(self):
        return OrderedDict([
            ("records", self.records), ("evaluated", self.evaluated), ("requests", self.requests),
            ("cached", self.cached), ("fresh", self.fresh), ("kv_hits", self.kv_hits), ("memo_hits", self.memo_hits),
            ("no_state", self.skipped_no_state), ("budget_exceeded", self.budget_exceeded),
            ("errors", dict(self.errors)), ("input_tokens", self.input_tokens), ("output_tokens", self.output_tokens),
            ("est_tokens", int(round(self.est_tokens))), ("kv_saved", self.kv_saved),
            ("p50_latency_ms", self.p50_latency()),
        ])


class Outcome(object):
    __slots__ = ("ok", "answer", "code", "message", "resolved_model", "tokens", "output_tokens",
                 "latency_ms", "attempts", "source")

    def __init__(self, ok, answer=None, code="", message="", resolved_model="", tokens=0.0, output_tokens=0.0,
                 latency_ms=0.0, attempts=0, source="fresh"):
        self.ok = ok
        self.answer = answer
        self.code = code
        self.message = message
        self.resolved_model = resolved_model
        self.tokens = tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.attempts = attempts
        self.source = source

    @classmethod
    def error(cls, code, message="", tokens=0.0):
        return cls(False, code=code, message=message or code, tokens=tokens, source="error")


def estimate_tokens(state, wires):
    """Rough input-token estimate for a request (dryrun); calibrate against real usage."""
    text = canonical_json(state) + json.dumps(wires, ensure_ascii=False)
    return len(text) / 3.3 + 20.0 * len(wires)


class _Plan(object):
    __slots__ = ("error", "state", "truncated", "wires", "keys", "digest")

    def __init__(self, error=None, state=None, truncated=False, wires=None, keys=None, digest=None):
        self.error = error
        self.state = state
        self.truncated = truncated
        self.wires = wires
        self.keys = keys          # qid -> (cache key, need key)
        self.digest = digest


class Evaluator(object):
    """Evaluate Splunk records against one battery (or one primitive question)."""

    def __init__(self, client, battery, model=None, threads=8, maxevents=5000, maxstate=16000,
                 prefix="jev_", probs=False, cache=True, keepstate=False, logger=None,
                 cache_mode=None, backend=None, ttl_days=0, batch_size=1000, dryrun=False,
                 primitive_alias=None, meta="basic", memo_limit=100000):
        self.client = client
        self.battery = battery
        self.model = model or client.model
        self.threads = max(1, int(threads))
        self.maxevents = int(maxevents or 0)
        self.maxstate = int(maxstate or 0)
        self.prefix = prefix
        self.probs = probs
        self.keepstate = keepstate
        self.cache_mode = cache_mode or ("memo" if cache else "off")
        self.backend = backend if self.cache_mode in ("kv", "refresh") else None
        self.ttl_days = ttl_days
        self.batch_size = max(1, int(batch_size or 1000))
        self.dryrun = bool(dryrun)
        self.alias = primitive_alias
        self.meta = meta if meta in ("basic", "full") else "basic"
        self.memo_limit = int(memo_limit)
        if self.alias:
            question = battery.questions[list(battery.questions)[0]]
            self.schema = primitive_schema(question, self.alias, probs, self.meta)
            if keepstate:
                self.schema.append(self.alias + "_state")
        else:
            self.schema = output_schema(battery, prefix, probs)
            if keepstate:
                self.schema.append(prefix + "state")
        self.memo = OrderedDict()
        self.kv_state = "ok" if self.backend is not None else "off"
        self.stats = Stats()
        self.warnings = []
        self._fresh = set()       # need-keys answered by a request in this search
        self._consumed = set()    # need-keys whose first consumer has been filled
        self._seq = 0
        self._log = logger

    # -- warnings shown once per search ------------------------------------------
    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def _debug(self, fmt, *args):
        if self._log is not None:
            self._log.warning(fmt, *args)

    # -- memo -----------------------------------------------------------------------
    def _remember(self, nkey, outcome):
        self.memo[nkey] = outcome
        self.memo.move_to_end(nkey)
        while len(self.memo) > self.memo_limit:
            old, _ = self.memo.popitem(last=False)
            self._fresh.discard(old)
            self._consumed.discard(old)

    # -- the work -----------------------------------------------------------------
    def _call(self, state, questions):
        try:
            return ("ok", self.client.judge(state, questions, self.model))
        except JevError as exc:
            return ("error", exc)
        except Exception as exc:  # never let a worker kill the search
            return ("error", JevError("internal", "%s: %s" % (type(exc).__name__, exc)))

    def _plan(self, records):
        plans = []
        need = OrderedDict()   # need key -> (qid, wire, state, cache key, group id)
        off = self.cache_mode == "off"
        for index, record in enumerate(records):
            state, truncated, error = build_state(record, self.battery.state, self.battery.required_state, self.maxstate)
            if state is None:
                plans.append(_Plan(error=error))
                continue
            wires = wire_questions(self.battery, state)
            if not wires:
                plans.append(_Plan(error="no_questions", state=state, truncated=truncated))
                continue
            keys = OrderedDict()
            group = ("rec", self._seq, index) if off else canonical_json(state)
            for qid, wire in wires.items():
                key = cache_key(self.model, state, wire)
                nkey = "%s#%d.%d" % (key, self._seq, index) if off else key
                keys[qid] = (key, nkey)
                if nkey in need or (not off and nkey in self.memo):
                    continue
                need[nkey] = (qid, wire, state, key, group)
            plans.append(_Plan(state=state, truncated=truncated, wires=wires, keys=keys,
                               digest=state_hash(state, wires, self.model)))
        self._seq += 1
        return plans, need

    def _read_backend(self, need):
        if self.backend is None or self.cache_mode != "kv" or self.kv_state == "unavailable" or not need:
            return
        wanted = []
        seen = set()
        for info in need.values():
            if info[3] not in seen:
                seen.add(info[3])
                wanted.append(info[3])
        started = time.time()
        try:
            hits = self.backend.get_many(wanted, self.ttl_days)
        except CacheUnavailable as exc:
            self.kv_state = "unavailable"
            self.warn("jev: judgment cache unavailable (%s); answers are judged fresh and kept only for this search" % exc)
            return
        except Exception as exc:  # the cache must never fail a search
            self.kv_state = "unavailable"
            self.warn("jev: judgment cache unavailable (%s: %s); answers are judged fresh and kept only for this search"
                      % (type(exc).__name__, exc))
            return
        finally:
            self.stats.kv_get_ms += (time.time() - started) * 1000.0
        for nkey, (qid, wire, state, key, group) in list(need.items()):
            doc = hits.get(key)
            if not doc:
                continue
            answer = doc_answer(doc)
            if not isinstance(answer, dict) or answer.get("type") not in (None, wire.get("type")):
                continue
            self._remember(nkey, Outcome(True, answer=answer, resolved_model=doc.get("resolved_model", ""),
                                         tokens=float(doc.get("input_tokens") or 0), source="kv"))
            del need[nkey]

    def _write_backend(self, docs):
        if not docs or self.backend is None or self.cache_mode not in ("kv", "refresh"):
            return
        if self.kv_state in ("unavailable", "readonly"):
            return
        started = time.time()
        try:
            for start in range(0, len(docs), self.batch_size):
                batch = docs[start:start + self.batch_size]
                try:
                    self.backend.put_many(batch)
                except CacheReadOnly:
                    raise
                except Exception:
                    self.backend.put_many(batch)  # one retry
                self.stats.kv_saved += len(batch)
        except CacheReadOnly as exc:
            self.kv_state = "readonly"
            self.warn("jev: cannot write the judgment cache (%s); answers will be judged again next time" % exc)
        except Exception as exc:
            self.kv_state = "readonly"
            self.warn("jev: writing the judgment cache failed (%s: %s); answers will be judged again next time"
                      % (type(exc).__name__, exc))
        finally:
            self.stats.kv_put_ms += (time.time() - started) * 1000.0

    def process(self, records):
        records = list(records)
        self.stats.records += len(records)
        plans, need = self._plan(records)
        self._read_backend(need)

        groups = OrderedDict()   # group id -> [state, OrderedDict(qid -> wire), OrderedDict(qid -> need key)]
        key_of = {}
        for nkey, (qid, wire, state, key, group) in need.items():
            entry = groups.setdefault(group, [state, OrderedDict(), OrderedDict()])
            entry[1][qid] = wire
            entry[2][qid] = nkey
            key_of[nkey] = key

        sendable = []
        for group, (state, wires, nkeys) in groups.items():
            if self.client.auth_dead:
                continue
            if self.dryrun:
                estimate = estimate_tokens(state, wires)
                self.stats.est_tokens += estimate
                for qid, nkey in nkeys.items():
                    self._remember(nkey, Outcome.error("dryrun", "dryrun: not sent", tokens=estimate / len(wires)))
                continue
            if self.maxevents and self.stats.requests + len(sendable) >= self.maxevents:
                continue
            sendable.append((state, wires, nkeys))

        to_save = []
        if sendable:
            with ThreadPoolExecutor(max_workers=min(self.threads, len(sendable))) as pool:
                futures = [(state, wires, nkeys, pool.submit(self._call, state, wires)) for state, wires, nkeys in sendable]
                for state, wires, nkeys, future in futures:
                    kind, value = future.result()
                    self.stats.requests += 1
                    if kind == "ok":
                        count = len(wires)
                        share = value.input_tokens / float(count)
                        out_share = value.output_tokens / float(count)
                        self.stats.input_tokens += value.input_tokens
                        self.stats.output_tokens += value.output_tokens
                        self.stats.latencies.append(value.latency_ms)
                        for qid, nkey in nkeys.items():
                            answer = value.answers.get(qid)
                            if not isinstance(answer, dict):
                                self._remember(nkey, Outcome.error("missing_answer", "no answer for %s" % qid))
                                continue
                            self._remember(nkey, Outcome(True, answer=answer, resolved_model=value.model, tokens=share,
                                                         output_tokens=out_share, latency_ms=value.latency_ms,
                                                         attempts=value.attempts, source="fresh"))
                            self._fresh.add(nkey)
                            to_save.append(make_doc(key_of[nkey], self.model, wires[qid], answer, value.model, share,
                                                    value.input_tokens, count, value.latency_ms))
                    else:
                        for qid, nkey in nkeys.items():
                            self._remember(nkey, Outcome.error(value.code, value.message))
                        if value.code == "auth_failed":
                            self.warn("jev: the TypeSafe API rejected the key; check the Jev for Splunk Setup page")
                        elif value.code.startswith("validation:"):
                            self.warn("jev: the API rejected the request (%s); check the question definition"
                                      % value.message[:160])
                        self._debug("judgment failed code=%s msg=%s", value.code, value.message[:200])
        self._write_backend(to_save)

        for record, plan in zip(records, plans):
            self._fill(record, plan)
        if self.cache_mode == "off":
            # nothing is reused in off mode; do not keep the per-record outcomes around
            for plan in plans:
                for _, nkey in (plan.keys or {}).values():
                    self.memo.pop(nkey, None)
                    self._fresh.discard(nkey)
                    self._consumed.discard(nkey)
        return records

    # -- output ---------------------------------------------------------------------
    def _consume(self, nkey, outcome):
        """Return True when this record is the first consumer of a fresh answer (i.e. it paid)."""
        first = nkey not in self._consumed
        self._consumed.add(nkey)
        if outcome.source == "fresh" and first and nkey in self._fresh:
            self.stats.fresh += 1
            return True
        if outcome.source == "kv" and first:
            self.stats.kv_hits += 1
        elif outcome.ok:
            self.stats.memo_hits += 1
        return False

    def _missing_code(self):
        if self.client.auth_dead:
            self.warn("jev: the TypeSafe API rejected the key; check the Jev for Splunk Setup page")
            return "auth_failed"
        self.warn("jev: maxevents=%d reached; remaining events carry an error of maxevents_exceeded" % self.maxevents)
        return "maxevents_exceeded"

    def _fill(self, record, plan):
        blank_fields(record, self.schema)
        state_field = (self.alias + "_state") if self.alias else (self.prefix + "state")
        if self.keepstate:
            record[state_field] = json.dumps(plan.state, ensure_ascii=False) if plan.state is not None else ""
        if plan.error:
            self.stats.skipped_no_state += 1
            self.stats.note_error(plan.error)
            if self.alias:
                fill_primitive_meta(record, self.alias, self.meta, error=plan.error, truncated=plan.truncated)
            else:
                fill_meta(record, self.prefix, self.battery, error=plan.error, truncated=plan.truncated)
            return

        first_error = ""
        paid_any = False
        all_reused = True
        tokens = 0.0
        output_tokens = 0.0
        model = ""
        latency = 0.0
        attempts = 0
        for qid, (key, nkey) in plan.keys.items():
            outcome = self.memo.get(nkey)
            if outcome is None:
                code = self._missing_code()
                if code == "maxevents_exceeded":
                    self.stats.budget_exceeded += 1
                first_error = first_error or code
                all_reused = False
                if self.alias:
                    fill_primitive_meta(record, self.alias, self.meta, error=code, key=key, truncated=plan.truncated)
                continue
            paid = self._consume(nkey, outcome) if outcome.ok else False
            question = self.battery.questions[qid]
            if outcome.ok:
                base = self.alias if self.alias else self.prefix + qid
                fill_answer(record, question, outcome.answer, base, self.probs)
                tokens += outcome.tokens
                output_tokens += outcome.output_tokens
                model = model or outcome.resolved_model
                latency = max(latency, outcome.latency_ms)
                attempts = max(attempts, outcome.attempts)
                paid_any = paid_any or paid
                all_reused = all_reused and not paid
            else:
                first_error = first_error or outcome.code
                all_reused = False
                if outcome.code == "dryrun":
                    tokens += outcome.tokens
            if self.alias:
                fill_primitive_meta(record, self.alias, self.meta, error="" if outcome.ok else outcome.code,
                                    cached=outcome.ok and not paid, tokens=outcome.tokens if (outcome.ok or outcome.code == "dryrun") else "",
                                    model=outcome.resolved_model, latency_ms=outcome.latency_ms if outcome.ok else "",
                                    attempts=outcome.attempts if outcome.ok else "", key=key, truncated=plan.truncated)

        if first_error:
            self.stats.note_error(first_error)
        else:
            self.stats.evaluated += 1
            if all_reused:
                self.stats.cached += 1
        if not self.alias:
            fill_meta(record, self.prefix, self.battery, model=model,
                      input_tokens=tokens if (tokens or not first_error) else "",
                      output_tokens=output_tokens if not first_error or output_tokens else "",
                      latency_ms=latency if model else "", error=first_error, state_hash=plan.digest,
                      truncated=plan.truncated, cached=(not first_error and all_reused), attempts=attempts if model else "")
