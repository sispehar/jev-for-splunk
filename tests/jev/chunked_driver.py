"""Drive a chunked (SCP v2) custom search command the way splunkd does.

    getinfo, chunks, stderr, dispatch_dir = run_command("jev_for_splunk/bin/jev.py", ["battery=b"], records, env)
    getinfo, chunks, ... = run_command(script, args, chunks=[[...], [...]], raw_args=[...], app="jev_shop_demo")

`chunks` is a list of (metadata, records) per output chunk. Messages that the
command wrote (write_info / write_warning / write_error) appear in
metadata["inspector"]["messages"]. With several input chunks the driver sends
one, reads the reply, then sends the next, as splunkd does.
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile


def encode_chunk(metadata, body=""):
    meta = json.dumps(metadata).encode("utf-8")
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    header = "chunked 1.0,%d,%d\n" % (len(meta), len(body_bytes))
    return header.encode("utf-8") + meta + body_bytes


def read_chunk(stream):
    header = stream.readline()
    if not header:
        return None, None
    text = header.decode("utf-8").strip()
    if not text.startswith("chunked 1.0,"):
        raise ValueError("unexpected header %r" % header)
    _, meta_len, body_len = text.split(",")
    meta = stream.read(int(meta_len))
    body = stream.read(int(body_len))
    return json.loads(meta.decode("utf-8")) if meta else {}, body.decode("utf-8")


def records_to_csv(records):
    fields = []
    for record in records:
        for key in record.keys():
            if key not in fields:
                fields.append(key)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({k: record.get(k, "") for k in fields})
    return out.getvalue()


def csv_to_records(text):
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    return [{k: v for k, v in row.items() if not k.startswith("__mv_")} for row in reader]


def searchinfo(args, dispatch_dir, app="jev_for_splunk", raw_args=None):
    return {
        "args": list(args), "raw_args": list(raw_args if raw_args is not None else args), "dispatch_dir": dispatch_dir,
        "sid": "driver_1", "app": app, "owner": "admin", "username": "admin", "session_key": "driver-session",
        "splunkd_uri": "https://127.0.0.1:8089", "splunk_version": "10.4.3", "search": "| " + " ".join(args),
        "command": args[0] if args else "", "maxresultrows": 50000, "earliest_time": "0", "latest_time": "0",
    }


def run_command(script, args, records=None, env=None, timeout=120, python=None, chunks=None, raw_args=None,
                app="jev_for_splunk"):
    """Spawn `script`, run getinfo + execute chunk(s); return (getinfo, [(metadata, records)], stderr, dispatch_dir)."""
    dispatch_dir = tempfile.mkdtemp(prefix="jev_dispatch_")
    proc_env = dict(os.environ)
    proc_env.update(env or {})
    proc_env.setdefault("SPLUNK_HOME", dispatch_dir)  # logging.conf writes under $SPLUNK_HOME/var/log/splunk
    os.makedirs(os.path.join(dispatch_dir, "var", "log", "splunk"), exist_ok=True)
    inputs = chunks if chunks is not None else [records or []]
    # Under SCP v2 splunkd starts the script WITHOUT command-line arguments; the
    # options travel inside the getinfo metadata (searchinfo.args). Passing them
    # on argv makes the SDK assume protocol v1 and refuse to run.
    proc = subprocess.Popen([python or sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=proc_env, cwd=os.path.dirname(os.path.abspath(script)))
    out = []
    try:
        getinfo = {"action": "getinfo", "preview": False, "streaming_command_will_restart": False,
                   "searchinfo": searchinfo(args, dispatch_dir, app=app, raw_args=raw_args)}
        proc.stdin.write(encode_chunk(getinfo))
        proc.stdin.flush()
        getinfo_meta, _ = read_chunk(proc.stdout)
        if getinfo_meta is None or getinfo_meta.get("finished") or getinfo_meta.get("error"):
            proc.stdin.close()
        else:
            for index, batch in enumerate(inputs):
                last = index == len(inputs) - 1
                proc.stdin.write(encode_chunk({"action": "execute", "finished": last}, records_to_csv(batch)))
                proc.stdin.flush()
                meta, body = read_chunk(proc.stdout)
                if meta is None:
                    break
                out.append((meta, csv_to_records(body)))
                if meta.get("finished"):
                    break
            proc.stdin.close()
            while True:
                meta, body = read_chunk(proc.stdout)
                if meta is None:
                    break
                out.append((meta, csv_to_records(body)))
        proc.wait(timeout=timeout)
        stderr = proc.stderr.read().decode("utf-8", "replace")
    finally:
        if proc.poll() is None:
            proc.kill()
    return getinfo_meta, out, stderr, dispatch_dir


def messages(meta):
    return [(m[0], m[1]) for m in (meta.get("inspector") or {}).get("messages", [])]


def all_messages(chunks):
    return [m for meta, _ in chunks for m in messages(meta)]


def all_records(chunks):
    return [r for _, rows in chunks for r in rows]
