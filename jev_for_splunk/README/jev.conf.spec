# jev.conf - settings for the | jev and | jevtest search commands.
# Ship-time defaults live in default/jev.conf; put overrides in local/jev.conf.
# The app a search runs in may ship its own jev.conf; it is layered on top.

[api]
endpoint = <string>
* Base URL of the TypeSafe API. The command posts to <endpoint>/systemone and
  reads <endpoint>/models.
* Default: https://api.typesafe.ai/v1

model = <string>
* Model sent in every request unless the search sets model=. Aliases such as
  jev-latest move with releases; pin a version id (for example jev-1.13.0) once
  thresholds are tuned.
* Default: jev-latest

timeout = <integer>
* Per-request timeout in seconds.
* Default: 30

max_retries = <integer>
* Retries per request on HTTP 429, 529, 5xx and network errors, with
  exponential backoff that honours Retry-After.
* Default: 5

requests_per_second = <number>
* Token-bucket cap on outbound requests per search process. TypeSafe's
  published limit is 1,200 requests per minute (20 per second).
* Default: 15

proxy_url = <string>
* Optional HTTPS proxy, for example http://proxy.example.com:3128.
* Default: empty (direct)

ca_bundle = <string>
* Optional path to a PEM bundle when Splunk's Python cannot verify the
  TypeSafe certificate with its default trust store.
* Default: empty (system default)

[defaults]
threads = <integer>
* Parallel requests per search chunk (1-32). The search may override with threads=.
* Default: 8

prefix = <string>
* Prefix of every output field in the battery form. The primitive form names
  its fields after the alias given with `as <name>`.
* Default: jev_

maxevents = <integer>
* Cost guard: maximum distinct states judged per search. Events beyond it pass
  through with jev_error=maxevents_exceeded. 0 disables the guard.
* Default: 5000

maxstate = <integer>
* Maximum serialized state size in characters; longer string values are
  truncated proportionally and jev_truncated=1 is set.
* Default: 16000

probs = <boolean>
* Emit per-option (choice) and per-level (score) probability fields by default.
* Default: false

[cache]
mode = kv|memo|refresh|off
* kv: reuse answers within the search and from the KV store collection, and
  store new ones. refresh: judge again and overwrite the stored answers.
  memo: reuse only within the search. off: never reuse, not even duplicates.
  A search can override it with cache=.
* Default: kv

collection = <string>
* KV store collection that holds the answers (see collections.conf).
* Default: jev_cache

ttl_days = <integer>
* Answers older than this are treated as missing and judged again, and the
  nightly saved search jev_purge_expired (| jevpurge) deletes them. 0 keeps
  them forever.
* Default: 90

batch_size = <integer>
* Documents per KV store batch_save call (the KV store accepts up to 1000).
* Default: 1000

[storage]
realm = <string>
* Realm of the storage/passwords entry that holds the TypeSafe API key.
* Default: jev_for_splunk

username = <string>
* Username of that storage/passwords entry.
* Default: typesafe_api_key
