# Architecture

```
 events (any index)
    |
 search head: ... | jev noul|choice|score ... --- api.typesafe.ai
                          |
                     KV store: jev_cache (one answer per model, state, question)
```

## The command

- **`bin/jev.py`** is a chunked (SCP v2) streaming command, declared non-distributed, so it runs on the
  search head where the key, the cache and the outbound route are.
  - It declares `required_fields = *`. The fields a question reads are named in its arguments, which
    Splunk's field optimizer cannot see.
  - It overrides splunklib's option parser: a token is an option only if its name is declared and it was
    not quoted. Questions can therefore contain `=`.
  - It layers the search's app's own `jev.conf` on top of its own. That lets an app pin a model.
- **`bin/jev_core/`** holds everything else, standard library only, and is tested without Splunk:
  - `primitive.py` builds the question sent to the model: ``Regarding `message`: <text>`` plus
    options or levels. The template is frozen, because it is part of every cache key.
  - `battery.py` handles batteries. A text-form question goes through the same builder, so a battery
    and the primitive form produce identical questions.
  - `cache.py` computes the key `sha256(schema, requested model, canonical state, canonical question)`.
    The question id is not part of the key, because TypeSafe never sends ids to the model. It also has
    the sqlite and memory backends.
  - `runner.py` handles each chunk:
    1. plan the records;
    2. reuse answers from the search's LRU memo;
    3. read the rest from the KV store in batches;
    4. group what is still missing by state, and send one request per state carrying only the missing
       questions;
    5. store successes;
    6. give every record every schema field (the chunked protocol takes the field list from the first
       record).
  - `client.py` retries 429 and 5xx with Retry-After, and stops the whole search after a 401.
- **`bin/jev_splunk.py`** reads the single storage/passwords entry and implements the KV store backend
  with `batch_find` and `batch_save`.
- **`bin/jevtest.py`** is the self-test behind the Setup page and the Health view: Python, splunklib, the
  config, batteries, the stored key, the API, the KV store, which app answers `| jev`, and with
  `live=true` one paid round trip.
