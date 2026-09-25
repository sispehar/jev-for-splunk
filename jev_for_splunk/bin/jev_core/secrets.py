"""Where the TypeSafe API key comes from outside Splunk.

Inside Splunk the key lives in storage/passwords and is read by
bin/jev_splunk.key_from_splunk with the searching user's session key. Outside
Splunk (tests, CLI, experiments) it comes from the TYPESAFE_API_KEY environment
variable or the nearest .env file.
"""
from __future__ import annotations

import hashlib
import os


def read_dotenv(path):
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip().strip('"').strip("'")
                values[key] = value
    except OSError:
        pass
    return values


def key_from_environment(env=None, start_dir=None):
    env = os.environ if env is None else env
    key = env.get("TYPESAFE_API_KEY") or env.get("JEV_API_KEY")
    if key:
        return key.strip()
    directory = os.path.abspath(start_dir or os.getcwd())
    for _ in range(6):
        values = read_dotenv(os.path.join(directory, ".env"))
        if values.get("TYPESAFE_API_KEY"):
            return values["TYPESAFE_API_KEY"]
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


def fingerprint(secret):
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:8]
