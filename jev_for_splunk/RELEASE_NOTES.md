# Release notes: Jev for Splunk

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
