# Jev for Splunk

`| jev` asks [TypeSafe Jev](https://docs.typesafe.ai) typed questions about each event and adds the answers
as fields. Jev is a calibrated "System One" model: it returns a probability, one of your options, or a
position on levels you describe, never generated text.

## The primitive form (one question)

```
... | jev noul  <fields> "<statement or question>" [criteria_true="..."] [criteria_false="..."] as <name>
... | jev choice <fields> "<question>" options=lookup:<[app:]name> | options="a: description; b: description" as <name>
... | jev score  <fields> "<question>" levels=lookup:<[app:]name> | levels="lowest; ...; highest" as <name>
```

- `<fields>` is `message`, `text=_raw`, or `a=field1,b=field2` (typed with `:num`, `:bool` or `:json`).
  Without it, the question reads `_raw`.
- Quote the question. It may contain `=`, but not backticks, because Splunk expands those as macros.
- Options lookups are CSV files with `option` and optional `description` columns, found in the search's
  app, then in this app, then in the one app that has it. Level lookups have a `level` column, lowest
  first.

| form | fields added |
|---|---|
| noul | `<name>` = P(yes) |
| choice | `<name>` (the chosen option), `<name>_confidence`, `<name>_p_<option>` with `probs=true` |
| score | `<name>` (the expected level), `<name>_level`, `<name>_label`, `<name>_confidence`, `<name>_p_<n>` with `probs=true` |
| always | `<name>_error`, `<name>_cached` (1 when this search did not pay for it), `<name>_tokens` (its share of its request), `<name>_model` |

Chained questions never collide: `| jev noul ... as a | jev choice ... as b` gives `a*` and `b*` fields.
`sum(eval(`jev_paid(a)`))` is what a search actually paid, and `jev_cost_usd(t)` turns tokens into dollars.

## The battery form (several questions in one request)

```
... | jev battery=shop_messages            # <app>/default/batteries/shop_messages.json (local/ overrides)
... | jev state="text=_raw" questions="{\"urgent\":{\"type\":\"noul\",\"instructions\":\"Does `text` convey urgency?\"}}"
```

Every question of a battery goes to Jev in one request per event. The questions run in parallel inside
Jev, which is many times cheaper than separate calls. Fields are named `jev_<question>` plus `jev_model`,
`jev_input_tokens`, `jev_error`, `jev_cached` and so on (change the prefix with `prefix=`). A battery
question can be written in *text form* (`"text": "...", "options": "lookup:..."`). It is then built exactly
like the primitive form, so a warm-up search running the battery and a dashboard using `| jev noul`
share cached answers.

## The judgment cache

Answers are stored per (model, state, question) in the KV store collection `jev_cache`, without the event
text. Asking again, or asking the same question from a dashboard, only pays for events that were never
judged.

| option | effect |
|---|---|
| `cache=kv` | reuse within the search and from the KV store, and store new answers (the default) |
| `cache=refresh` | judge again and overwrite |
| `cache=memo` | reuse only within the search |
| `cache=off` | never reuse |
| `dryrun=true` | estimate tokens and cost without calling the API; cached answers are still filled in |

If the KV store is unavailable, the command warns once and carries on without it. The cache never fails a
search. `ttl_days` (90) in `jev.conf [cache]` expires old answers, and
`| inputlookup jev_cache_lookup` shows what is stored.

## Other options

`model=` (pin a version, e.g. `jev-1.13.0`; an app can also pin one in its own `default/jev.conf`),
`threads=` (1-32), `rps=` (requests per second, 1-20; TypeSafe allows 1,200 a minute), `maxevents=` (a cost
guard: at most this many API requests per search), `maxstate=`, `timeout=`, `probs=`, `meta=full`,
`keepstate=`. Defaults are in `default/jev.conf`, documented in `README/jev.conf.spec`.

## Setup and permissions

1. Open the **Setup** page, paste the TypeSafe API key, and run the self-test (`| jevtest live=true`).
   The key is stored in `storage/passwords` (realm `jev_for_splunk`).
2. Running `| jev` needs the `list_storage_passwords` capability, because the command reads the key with
   the searching user's own session. The KV collection is readable and writable by admin and sc_admin.
3. `| jev` runs only on the search head: the key, the cache and the outbound HTTPS route live there.
   It sends only the fields you name to `api.typesafe.ai`.

**Coming from `jev_agent_evals`:** it defines `| jev` too, so disable one of the two apps. `| jevtest`
reports which app answers the command (`command_owner`). The key realm changed to `jev_for_splunk`.

The **Health** view shows the self-test, fresh answers against cached ones, cost by app and user, and
warnings from `jev.log` (`` `jev_internal_log` ``).
