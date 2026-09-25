"""Find batteries and option/level lookups across Splunk apps, from files only.

References look like `name` or `app:name`. Without an app, the search's own app
is tried first, then jev_for_splunk, then every app; the scan must find exactly
one match. Reading files (instead of calling splunkd) keeps the command fast
and testable; lookups are read on the search head where the command runs.
"""
from __future__ import annotations

import csv
import glob
import io
import os
import re
from collections import OrderedDict

from .primitive import PrimitiveError, normalize_text, parse_inline_levels, parse_inline_options

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_LOOKUP_BYTES = 1024 * 1024
MAX_CHOICE_OPTIONS = 255
SCORE_LEVELS = (2, 10)
OWN_APP = "jev_for_splunk"


class ResolveError(ValueError):
    pass


def split_ref(ref):
    """'app:name' -> ('app', 'name'); 'name' -> (None, 'name')."""
    text = str(ref or "").strip()
    if ":" in text:
        app, name = text.split(":", 1)
    else:
        app, name = None, text
    for part in (app, name):
        if part is not None and not NAME_RE.match(part):
            raise ResolveError("invalid name %r (letters, digits, '.', '_' and '-' only)" % part)
    if not name:
        raise ResolveError("empty reference")
    return app, name


class FileLookupResolver(object):
    def __init__(self, apps_dir, search_app=None, own_app=OWN_APP, scan=True):
        self.apps_dir = os.path.abspath(apps_dir) if apps_dir else None
        self.search_app = search_app
        self.own_app = own_app
        self.scan = scan

    @classmethod
    def for_app_root(cls, app_root):
        """Resolve only inside one app directory (used by tests and the CLI's --app-root)."""
        app_root = os.path.abspath(app_root)
        name = os.path.basename(app_root)
        return cls(os.path.dirname(app_root), search_app=name, own_app=name, scan=False)

    # -- where to look ----------------------------------------------------------
    def _candidate_apps(self, app):
        if app:
            return [app]
        ordered = []
        for name in (self.search_app, self.own_app):
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    def _all_apps(self):
        if not self.apps_dir or not os.path.isdir(self.apps_dir):
            return []
        return sorted(d for d in os.listdir(self.apps_dir) if os.path.isdir(os.path.join(self.apps_dir, d)))

    def _find(self, app, relative_paths, what, ref):
        if not self.apps_dir:
            raise ResolveError("cannot resolve %s %r: no apps directory" % (what, ref))
        for name in self._candidate_apps(app):
            for rel in relative_paths:
                path = os.path.join(self.apps_dir, name, rel)
                if os.path.isfile(path):
                    return path
        if app is None and self.scan:
            hits = []
            for name in self._all_apps():
                for rel in relative_paths:
                    path = os.path.join(self.apps_dir, name, rel)
                    if os.path.isfile(path):
                        hits.append((name, path))
                        break
            if len(hits) == 1:
                return hits[0][1]
            if len(hits) > 1:
                raise ResolveError("%s %r exists in several apps (%s); write it as app:%s" % (
                    what, ref, ", ".join(h[0] for h in hits), ref))
        raise ResolveError("unknown %s %r" % (what, ref))

    # -- batteries ----------------------------------------------------------------
    def battery_path(self, ref):
        app, name = split_ref(ref)
        # local/ overrides default/ in the same app
        return self._find(app, [os.path.join("local", "batteries", name + ".json"),
                                os.path.join("default", "batteries", name + ".json")], "battery", ref)

    def list_batteries(self):
        """{app:id -> path} for every battery in the apps this resolver can see."""
        found = OrderedDict()
        apps = self._all_apps() if self.scan else self._candidate_apps(None)
        for app in apps:
            for sub in ("default", "local"):
                for path in sorted(glob.glob(os.path.join(self.apps_dir, app, sub, "batteries", "*.json"))):
                    found["%s:%s" % (app, os.path.splitext(os.path.basename(path))[0])] = path
        return found

    # -- lookups ----------------------------------------------------------------------
    def lookup_path(self, ref):
        app, name = split_ref(ref)
        filename = name if name.lower().endswith(".csv") else name + ".csv"
        return self._find(app, [os.path.join("lookups", filename)], "lookup", ref)

    def read_lookup(self, ref):
        path = self.lookup_path(ref)
        if os.path.getsize(path) > MAX_LOOKUP_BYTES:
            raise ResolveError("lookup %r is larger than 1 MB" % ref)
        with io.open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        rows = [r for r in rows if any(cell.strip() for cell in r)]
        if not rows:
            raise ResolveError("lookup %r is empty" % ref)
        header = [h.strip().lower() for h in rows[0]]
        return header, rows[1:]

    def options(self, spec):
        """options= value -> OrderedDict option -> description (or None)."""
        if isinstance(spec, dict):
            options = OrderedDict((str(k), (normalize_text(v) or None) if v is not None else None) for k, v in spec.items())
        elif str(spec).startswith("lookup:"):
            ref = str(spec)[len("lookup:"):]
            header, rows = self.read_lookup(ref)
            oi = header.index("option") if "option" in header else 0
            di = header.index("description") if "description" in header else (1 if len(header) > 1 else None)
            options = OrderedDict()
            for row in rows:
                name = row[oi].strip() if oi < len(row) else ""
                if not name:
                    continue
                if name in options:
                    raise ResolveError("lookup %r lists option %r twice" % (ref, name))
                description = normalize_text(row[di]) if di is not None and di < len(row) else ""
                options[name] = description or None
        else:
            try:
                options = parse_inline_options(spec)
            except PrimitiveError as exc:
                raise ResolveError(str(exc))
        if not (2 <= len(options) <= MAX_CHOICE_OPTIONS):
            raise ResolveError("a choice needs 2 to %d options; got %d" % (MAX_CHOICE_OPTIONS, len(options)))
        return options

    def levels(self, spec):
        """levels= value -> list of level descriptions, lowest first."""
        if isinstance(spec, (list, tuple)):
            levels = [normalize_text(v) for v in spec if normalize_text(v)]
        elif str(spec).startswith("lookup:"):
            ref = str(spec)[len("lookup:"):]
            header, rows = self.read_lookup(ref)
            li = header.index("level") if "level" in header else 0
            levels = [normalize_text(row[li]) for row in rows if li < len(row) and normalize_text(row[li])]
        else:
            levels = parse_inline_levels(spec)
        if not (SCORE_LEVELS[0] <= len(levels) <= SCORE_LEVELS[1]):
            raise ResolveError("a score needs %d to %d levels; got %d" % (SCORE_LEVELS[0], SCORE_LEVELS[1], len(levels)))
        return levels
