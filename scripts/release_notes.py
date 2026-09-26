#!/usr/bin/env python3
"""Write the GitHub release notes for a built package, in the shape of the v0.1.0 release.

    python3 scripts/release_notes.py dist/jev_for_splunk-0.1.1.tar.gz dist/appinspect-jev_for_splunk.json > notes.md

"What's new" is the top entry of jev_for_splunk/RELEASE_NOTES.md; the version and build come from
default/app.conf, which the package was built from.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
APP = os.path.join(REPO, "jev_for_splunk")
REPO_URL = "https://github.com/sispehar/jev-for-splunk"


def app_conf(key):
    text = open(os.path.join(APP, "default", "app.conf"), encoding="utf-8").read()
    return re.search(r"^%s\s*=\s*(\S+)" % key, text, re.M).group(1)


def whats_new():
    """The body of the first '## ' entry of RELEASE_NOTES.md."""
    text = open(os.path.join(APP, "RELEASE_NOTES.md"), encoding="utf-8").read()
    entry = re.split(r"^## .*$", text, flags=re.M)[1]
    return entry.strip()


def main(package, report):
    version, build = app_conf("version"), app_conf("build")
    sha256 = hashlib.sha256(open(package, "rb").read()).hexdigest()
    summary = json.load(open(report, encoding="utf-8"))["summary"]
    commit = os.environ.get("GITHUB_SHA") or subprocess.run(
        ["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    name = os.path.basename(package)
    print("""The `| jev` search command for Splunk: ask [TypeSafe Jev](https://docs.typesafe.ai) typed questions about any events from SPL and get the answers as fields.

```
index=support sourcetype=ticket
| jev noul message "The customer says they were charged more than they should have been" as overcharged
| timechart span=1h sum(overcharged)
```

## Install

1. Download `{name}` below.
2. Install it on the search head: Apps > Manage Apps > Install app from file (tick *Upgrade app* when upgrading), then restart Splunk. Indexers and forwarders don't need it.
3. Open **Jev for Splunk > Setup**, paste your TypeSafe API key ([console.typesafe.ai](https://console.typesafe.ai)) and run the self-test.
4. Give the people who run `| jev` the `jev_user` role.

Requirements: Splunk Enterprise 9.3 or later, the KV store enabled on the search head, and outbound HTTPS from the search head to `api.typesafe.ai`. The [README]({url}#readme) covers permissions and how to check the install. This app is not on Splunkbase: *Jev for Splunk (unofficial)* there is a different app with the same folder name.

## What's in {version}

{new}

## Package

- `{name}`: version {version}, build {build}, built by GitHub Actions from commit {commit}, with the vendored splunk-sdk 2.1.1 (Apache-2.0).
- AppInspect (cloud tags): {failures} failures, {warnings} warnings.
- SHA-256: `{sha256}`""".format(name=name, url=REPO_URL, version=version, new=whats_new(), build=build,
                                commit=commit[:7], failures=summary.get("failure", "?"),
                                warnings=summary.get("warning", "?"), sha256=sha256))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
