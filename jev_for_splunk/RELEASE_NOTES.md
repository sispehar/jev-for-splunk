# Release notes: Jev for Splunk

## 0.1.1

Restart Splunk after upgrading: splunkd loads the new key endpoint and the `jev_user` role at startup.

- **Fix: a long search could lose answers it already had.** The in-search memo holds at most 100,000
  answers. Once it was full, new answers could evict ones the same chunk still needed, including answers
  just paid for or read from the KV store, and those events reported `maxevents_exceeded`. The memo is
  now trimmed only after a chunk is filled, and a reused answer counts as recently used.
- **`| jev` no longer needs `list_storage_passwords`.** Give users the new `jev_user` role, which holds the
  `use_jev` capability and can read and write the judgment cache. The command gets the key from the app's
  own endpoint, which splunkd opens only to `use_jev` and which serves that key and no other secret.
  Dashboards that use `| jev` can now be shared with people who are not admins. Admins keep reading the
  key directly.
- **`| jevpurge`** deletes answers older than `ttl_days`, and the saved search `jev_purge_expired` runs it
  every night. The cache no longer grows without bound. Neither sends anything to TypeSafe.
- **An audit trail.** Every search's summary line in `jev.log` names the fields it sent (`state_fields`),
  and the Health view has a panel of what left Splunk, by user and app.
- **Settings on the Setup page:** the model, `maxevents`, `ttl_days`, the proxy and the CA bundle, saved
  to `local/jev.conf`.
- **`dryrun=true` works before a key is saved**, so the cost can be estimated before signing up.
- **Fix: a key stored outside the Setup page left the app unconfigured.** A key saved over REST or the
  CLI never set `is_configured`, so Splunk kept sending everyone who opened the app to the Setup page. The
  page now marks the app configured whenever it finds a stored key.
- **Fix: every self-test made splunkd log "Connection closed by peer"**, because the KV store probe's
  DELETE reply was never read. The Health view runs the self-test on every load.
- **Fix: a response cut short mid-body is retried** like other network errors. It used to fail the
  request with `internal`.
- **A malformed `jev.conf` value is reported by file and setting.** `| jev` shows the message, and
  `| jevtest` reports it as a failed `config` check and runs the other checks. A failed `live=true` probe
  is now reported as `api_live`, not `api_models`.
- **A dotted field name gets a hint:** `| jev noul data.message "..."` now says to write
  `msg=data.message`.
- **Lookup and battery references reject `.` and `..`** as app or file names.
- **Packaging:** app icons, a note on the Setup page about what leaves Splunk, the splunk-sdk download
  checked against its SHA-256, and GitHub Actions that test every change and publish tagged releases.

## 0.1.0, build 6

- **Fix: `| jev` followed by `stats`, `timechart` or `chart` judged nothing.** The command now declares
  `required_fields = *`. The field a question reads is named in its arguments, which Splunk's field
  optimizer cannot see, so in fast and smart mode a transforming command downstream kept the field from
  being extracted and every event was skipped for lack of state.

## 0.1.0, build 5

- **Fix: the stored API key was never found.** The reader URL-encoded the credential name and splunklib
  encoded it again (`%253A`), so splunkd answered 404 and `| jev` and the self-test reported
  `no TypeSafe API key stored yet` even after Setup saved it. A regression test now runs the path
  through splunklib's own quoting.

## 0.1.0

- **The primitive form.** `| jev noul|choice|score <fields> "<question>" as <name>` asks one typed
  question per pipe stage. Options and levels come from lookups or inline lists. Fields are named per
  alias, so chained questions never collide.
- **A judgment cache in the KV store** (`jev_cache`), one answer per (model, state, question). Asking
  again, or asking from a dashboard, only pays for new events. Modes: `cache=kv|refresh|memo|off`.
  `cache=off` now really disables reuse.
- **Text-form battery questions.** They are built exactly like the primitive form and share cache entries
  with it.
- **`dryrun=true`** estimates tokens and cost without calling the API.
- **Questions may contain `=`.** Only a declared, unquoted `name=value` token is an option.
- **Per-app model pinning.** An app can ship its own `default/jev.conf`, which is layered on top for
  searches in that app.
- **`| jevtest`** checks the KV store and which app answers `| jev`. The **Health** view shows fresh
  against cached answers and cost by app and user.
- The key is read as a single storage/passwords entry. It used to be found by listing them all.
- Extracted from `jev_agent_evals`, without its agent-specific batteries, macros and views.

Packaging: splunk-sdk 2.1.1 vendored (Apache-2.0); Python 3.9 and 3.13.
