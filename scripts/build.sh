#!/usr/bin/env bash
# Build the Jev for Splunk app.
#
# Usage: ./scripts/build.sh [--release [patch|minor]] [--keep-build] [--appinspect] [--vendor-sdk] [--skip-tests]
#
#   (no flags)     dev build: version unchanged, [install] build bumped
#   --release      bump the version (app.conf, app.manifest and jev_core)
#   --keep-build   package the version and build as committed (CI and GitHub releases)
#   --appinspect   run splunk-appinspect (cloud tags) on the tarball; fail on any failure
#   --vendor-sdk   (re)download splunk-sdk and refresh bin/lib/splunklib
#   --skip-tests   skip pytest (still validates batteries and Python 3.9 syntax)
#
# Output: dist/jev_for_splunk.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
APP="jev_for_splunk"
SDK_VERSION="2.1.1"
# sha256 of splunk-sdk-2.1.1.tar.gz on PyPI; a download that does not match is never vendored
SDK_SHA256="46300d52f09e0aed7e5962ce2ba08ef54421ffb3a538c6af6164dcbf9f075faa"

RELEASE=""; RUN_APPINSPECT=0; VENDOR=0; TESTS=1; KEEP_BUILD=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --release) RELEASE="patch"; if [ $# -gt 1 ] && { [ "$2" == "patch" ] || [ "$2" == "minor" ]; }; then RELEASE="$2"; shift; fi ;;
        --keep-build) KEEP_BUILD=1 ;;
        --appinspect) RUN_APPINSPECT=1 ;;
        --vendor-sdk) VENDOR=1 ;;
        --skip-tests) TESTS=0 ;;
        *) echo "Unknown argument: $1 (see --help)"; exit 1 ;;
    esac
    shift
done

PY="$(command -v python3)"
sedi() { if [[ "$(uname)" == "Darwin" ]]; then sed -i '' "$@"; else sed -i "$@"; fi; }
echo "=== Jev for Splunk: build ==="

# 1. Vendored splunklib (Apache-2.0)
if [ "$VENDOR" -eq 1 ] || [ ! -f "$REPO/$APP/bin/lib/splunklib/__init__.py" ]; then
    echo "Vendoring splunk-sdk $SDK_VERSION..."
    TMP="$(mktemp -d)"
    URL="$(curl -s "https://pypi.org/pypi/splunk-sdk/$SDK_VERSION/json" | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print([u["url"] for u in d["urls"] if u["packagetype"]=="sdist"][0])')"
    curl -sL -o "$TMP/sdk.tar.gz" "$URL"
    GOT="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$TMP/sdk.tar.gz")"
    [ "$GOT" == "$SDK_SHA256" ] || { echo "ERROR: splunk-sdk $SDK_VERSION checksum mismatch (got $GOT)"; rm -rf "$TMP"; exit 1; }
    tar xzf "$TMP/sdk.tar.gz" -C "$TMP"
    rm -rf "$REPO/$APP/bin/lib/splunklib"
    mkdir -p "$REPO/$APP/bin/lib"
    cp -R "$TMP"/splunk-sdk-*/splunklib "$REPO/$APP/bin/lib/splunklib"
    rm -rf "$TMP"
    echo "  + splunklib $SDK_VERSION"
fi

# 2. Generated files: the health view and nav
"$PY" -B "$REPO/scripts/gen_dashboards.py"

# 3. Batteries (any app under the repository root) must be valid before they ship
( cd "$REPO/$APP/bin" && "$PY" -B -m jev_core.cli --apps-dir "$REPO" validate ) || { echo "ERROR: battery validation failed"; exit 1; }
echo "  + batteries valid"

# 4. Tests on both Python versions Splunk ships
if [ "$TESTS" -eq 1 ] && command -v uv >/dev/null 2>&1; then
    for v in 3.9 3.13; do
        OUT="$(cd "$REPO" && uv run --python "$v" --no-project --with pytest pytest -q -p no:cacheprovider 2>&1)" \
            || { echo "$OUT" | tail -40; echo "ERROR: tests failed on Python $v"; exit 1; }
        echo "  + tests pass on Python $v"
    done
fi

# 5. Python 3.9 syntax of everything we ship (Splunk 10.0/10.1 bundle 3.9)
if command -v uv >/dev/null 2>&1; then
    uv run --python 3.9 --no-project python -m compileall -q "$REPO/$APP/bin/"*.py "$REPO/$APP/bin/jev_core" >/dev/null \
        || { echo "ERROR: not Python 3.9 compatible"; exit 1; }
    echo "  + python 3.9 syntax ok"
fi

# 6. One version; the build number moves on every build (Splunk caches static files by it)
CONF="$REPO/$APP/default/app.conf"
CUR="$(grep '^version' "$CONF" | head -1 | cut -d= -f2 | tr -d ' ')"
NEW="$CUR"
if [ -n "$RELEASE" ]; then
    MAJ="$(echo "$CUR" | cut -d. -f1)"; MIN="$(echo "$CUR" | cut -d. -f2)"; PAT="$(echo "$CUR" | cut -d. -f3)"
    case "$RELEASE" in minor) NEW="${MAJ}.$((MIN + 1)).0" ;; patch) NEW="${MAJ}.${MIN}.$((PAT + 1))" ;; esac
fi
sedi "s/^version = .*/version = ${NEW}/" "$CONF"
CURB="$(grep '^build = ' "$CONF" | head -1 | cut -d= -f2 | tr -d ' ')"
NEWB=$((CURB + 1))
if [ "$KEEP_BUILD" -eq 1 ]; then NEWB="$CURB"; fi
sedi "s/^build = .*/build = ${NEWB}/" "$CONF"
"$PY" - "$REPO/$APP/app.manifest" "$NEW" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
doc = json.load(open(path)); doc["info"]["id"]["version"] = version
open(path, "w").write(json.dumps(doc, indent=2) + "\n")
PY
sedi "s/^__version__ = .*/__version__ = \"${NEW}\"/" "$REPO/$APP/bin/jev_core/__init__.py"
sedi "s/^version = .*/version = \"${NEW}\"/" "$REPO/pyproject.toml"
"$PY" - "$REPO/$APP" <<'PY' || exit 1
import json, pathlib, re, sys
app = pathlib.Path(sys.argv[1])
versions = set(re.findall(r'^version\s*=\s*(\S+)', (app / "default/app.conf").read_text(), re.M))
versions.add(json.load(open(app / "app.manifest"))["info"]["id"]["version"])
versions.add(re.search(r'__version__ = "([^"]+)"', (app / "bin/jev_core/__init__.py").read_text()).group(1))
if len(versions) != 1:
    print("ERROR: version mismatch: %s" % sorted(versions)); raise SystemExit(1)
PY
echo "  version: $CUR -> $NEW$([ -n "$RELEASE" ] && echo " (release)" || echo " (unchanged; use --release to bump)"), build $NEWB"

# 7. License and docs in the app; no bytecode, no local state
cp "$REPO/LICENSE" "$REPO/$APP/LICENSE"
for doc in README.md RELEASE_NOTES.md THIRD-PARTY-LICENSES.txt; do
    [ -f "$REPO/$APP/$doc" ] || { echo "  ! $APP is missing $doc"; [ -n "$RELEASE" ] && exit 1; }
done
find "$REPO/$APP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$REPO/$APP" -name '*.pyc' -delete 2>/dev/null || true

# 8. Package
mkdir -p "$REPO/dist"
TAR_FLAGS=()
if [[ "$(uname)" == "Darwin" ]]; then
    export COPYFILE_DISABLE=1
    TAR_FLAGS+=(--disable-copyfile --no-xattrs --no-mac-metadata)
    xattr -rc "$REPO/$APP" 2>/dev/null || true
fi
tar ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} \
    --exclude='.git' --exclude='.git*' --exclude='.DS_Store' --exclude='._*' --exclude='__MACOSX' \
    --exclude="$APP/local" --exclude='local.meta' --exclude='__pycache__' --exclude='*.pyc' \
    -czf "$REPO/dist/$APP.tar.gz" -C "$REPO" "$APP"
SHA="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$REPO/dist/$APP.tar.gz")"
echo "  + dist/$APP.tar.gz ($(wc -c < "$REPO/dist/$APP.tar.gz" | tr -d ' ') bytes, sha256 $SHA)"

# 9. AppInspect
if [ "$RUN_APPINSPECT" -eq 1 ]; then
    AI_BIN="$(command -v splunk-appinspect || echo "$HOME/.local/bin/splunk-appinspect")"
    if [ ! -x "$AI_BIN" ]; then
        echo "  ! splunk-appinspect not found (uv tool install splunk-appinspect); skipping"
    else
        REPORT="$REPO/dist/appinspect-$APP.json"
        "$AI_BIN" inspect "$REPO/dist/$APP.tar.gz" --included-tags cloud --included-tags splunk_appinspect \
            --data-format json --output-file "$REPORT" >/dev/null 2>&1 || true
        FAILS="$("$PY" -c "import json;print(json.load(open('$REPORT'))['summary'].get('failure','?'))" 2>/dev/null || echo '?')"
        WARN="$("$PY" -c "import json;print(json.load(open('$REPORT'))['summary'].get('warning','?'))" 2>/dev/null || echo '?')"
        FUTURE="$("$PY" -c "import json;print(json.load(open('$REPORT'))['summary'].get('future_failure',0))" 2>/dev/null || echo '?')"
        echo "  AppInspect $APP: failure=$FAILS warning=$WARN future_failure=$FUTURE (dist/appinspect-$APP.json)"
        [ "$FAILS" == "0" ] || { echo "ERROR: AppInspect reported failures for $APP"; exit 1; }
        [ "$FUTURE" == "0" ] || echo "  ! AppInspect will fail $APP in a future release; see future_failure in the report"
    fi
fi
echo "Done. Install dist/$APP.tar.gz with Apps > Manage Apps > Install app from file."
