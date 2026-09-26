#!/usr/bin/env python
"""| jevpurge [days=<n>] - delete cached answers older than ttl_days from the judgment cache.

ttl_days already makes older answers invisible to | jev; this frees the space. The saved search
jev_purge_expired runs it nightly. Nothing is sent to TypeSafe.
"""
from __future__ import annotations

import os
import sys
import time

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(BIN_DIR)
for _path in (os.path.join(BIN_DIR, "lib"), BIN_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from splunklib.searchcommands import Configuration, GeneratingCommand, Option, dispatch, validators  # noqa: E402

from jev_core.cache import CacheReadOnly, CacheUnavailable, SqliteCache  # noqa: E402
from jev_core.config import ConfigError, load_config  # noqa: E402
from jev_splunk import KVStoreCache, service_for  # noqa: E402

OWN_APP = os.path.basename(APP_ROOT)


@Configuration(type="events")
class JevPurgeCommand(GeneratingCommand):
    days = Option(require=False, validate=validators.Integer(1))

    def generate(self):
        now = time.time()

        def row(status, detail, **extra):
            record = {"_time": now, "_raw": "status=%s detail=%s" % (status, detail), "status": status, "detail": detail}
            record.update(extra)
            return record

        try:
            cfg = load_config(APP_ROOT)
        except ConfigError as exc:
            yield row("fail", str(exc))
            return
        days = cfg.cache_ttl_days if self.days is None else self.days
        if not days:
            yield row("skipped", "ttl_days is 0, so answers never expire; pass days= to purge anyway")
            return
        cutoff = int(now - days * 86400)
        # JEV_CACHE_PATH is a development override used by the tests; Splunk never sets it.
        dev_cache = os.environ.get("JEV_CACHE_PATH")
        backend = SqliteCache(dev_cache) if dev_cache else KVStoreCache(
            service_for(self._metadata.searchinfo, OWN_APP), collection=cfg.cache_collection, app=OWN_APP)
        before = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
        try:
            deleted = backend.purge(cutoff)
        except (CacheReadOnly, CacheUnavailable) as exc:
            self.logger.error("purge failed collection=%s error=%s", cfg.cache_collection, exc)
            yield row("fail", "could not purge %s: %s" % (cfg.cache_collection, exc))
            return
        self.logger.info("purge collection=%s days=%d cutoff=%d deleted=%s user=%s", cfg.cache_collection, days,
                         cutoff, "unknown" if deleted is None else deleted,
                         getattr(self._metadata.searchinfo, "username", ""))
        counted = "" if deleted is None else "%d " % deleted
        yield row("ok", "deleted %sanswers created before %s from %s" % (counted, before, cfg.cache_collection),
                  collection=cfg.cache_collection, days=days, cutoff=cutoff,
                  deleted="" if deleted is None else deleted)


dispatch(JevPurgeCommand, sys.argv, sys.stdin, sys.stdout, __name__)
