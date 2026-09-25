# Jev for Splunk

`| jev` asks [TypeSafe Jev](https://docs.typesafe.ai) typed questions about your events and adds the
answers as fields. Jev is a calibrated "System One" model: it returns a probability, one of your
options, or a position on levels you describe, never generated text.

```
index=support sourcetype=ticket
| jev noul message "The customer says they were charged more than they should have been" as overcharged
| timechart span=1h sum(overcharged)
```

| question | Jev returns | what SPL does with it |
|---|---|---|
| `noul`: is this true? | a probability | **sums** it: the sum of probabilities is an expected count, so a sentence becomes a metric |
| `choice`: which of these? | one of your options and a confidence | **groups** or **joins** on it |
| `score`: how much? | a position on levels you describe | **ranks** by it |

```
... | jev choice message "Which part of the shop is this message about?" options="pricing: prices and discounts; delivery: shipping and tracking; other" as area | stats count BY area
... | jev score message "How upset is the customer?" levels="Calm question; Annoyed; Angry" as upset | sort - upset
```

Answers are cached in the KV store, one per model, question and state (the fields you name), without
the event text. Asking again, or asking the same question from a dashboard, only pays for events that
were never judged. The full command reference, including batteries (several questions in one request),
is in [jev_for_splunk/README.md](jev_for_splunk/README.md).

## Install

Requirements: Splunk Enterprise 9.x or 10.x with Python 3.9 or 3.13, the KV store enabled on the search
head, outbound HTTPS from the search head to `api.typesafe.ai`, and a TypeSafe API key
([console.typesafe.ai](https://console.typesafe.ai)).

1. Build the package:
   ```bash
   ./scripts/build.sh --appinspect     # dist/jev_for_splunk.tar.gz
   ```
2. Install it on the search head (Apps > Manage Apps > Install app from file). Indexers and forwarders
   don't need it.
3. Open **Jev for Splunk > Setup**, paste the TypeSafe key and run the self-test.
4. Read [Setup and permissions](jev_for_splunk/README.md#setup-and-permissions) for the capability
   `| jev` needs and who can write the cache.

`| jev` runs only on the search head, where the key, the cache and the outbound route are. It sends only
the fields you name to `api.typesafe.ai`.

## Check the install

| check | SPL | expected |
|---|---|---|
| self-test | `\| jevtest live=true` | all ok, including `kvstore` and `command_owner=jev_for_splunk` |
| a question | `\| makeresults \| eval message="My 20% code was accepted but I paid full price" \| jev noul message "The customer says they were charged more than they should have been" as overcharged` | `overcharged` above 0.8; run it again: `overcharged_cached=1` |
| `=` in a question | `\| makeresults \| eval message="x" \| jev noul message "Is 2+2=4 stated?" as eq` | no "Unrecognized option" |

The **Health** view shows the self-test, fresh answers against cached ones, cost by app and user, and
warnings from `jev.log`.

## Limits

- **Jev reads literally.** It does not count, do arithmetic or compare dates, and accuracy drops when the
  relevant text is buried in a large state. Keep states small (one message, one ticket) and leave the
  maths to SPL. See TypeSafe's [Jev 1.13 jaggedness page](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
- **Event text is data, not instructions.** Text written to steer a model can move an answer. Keep answer
  spaces small and validate on your own data before acting on a threshold.
- **Pin the model for anything that lasts.** Answers are cached per model name, and the default is
  `jev-latest`. Pin a version (`model=jev-1.13.0`, or `model` under `[api]` in your app's
  `default/jev.conf`) so a new release cannot put a step into a chart.
- **Every fresh answer is a paid request.** `maxevents` (5,000 by default) caps the API requests one
  search can make, and `dryrun=true` estimates tokens and cost without calling the API.

## Development

The tests need the vendored splunk-sdk. The first `./scripts/build.sh` downloads splunk-sdk 2.1.1 into
`jev_for_splunk/bin/lib` (gitignored). Every build bumps `build` in `default/app.conf`, because Splunk
caches static files by it.

```bash
./scripts/build.sh --appinspect                              # tests on 3.9 and 3.13, AppInspect, the tarball
uv run --python 3.13 --no-project --with pytest pytest -q    # and --python 3.9
cd jev_for_splunk/bin && TYPESAFE_API_KEY=... python3 -m jev_core.cli ask noul message \
    "The customer says they were charged more than they should have been" --as overcharged --records rows.ndjson
```

The last line runs the same code as the Splunk command, on NDJSON records, without Splunk.

| path | what it is |
|---|---|
| `jev_for_splunk/` | the Splunk app: `\| jev` (noul, choice, score and batteries), a KV store judgment cache, a setup page, `\| jevtest`, a health view |
| `jev_for_splunk/bin/jev_core/` | the logic, standard library only, tested without Splunk |
| `scripts/` | `build.sh` and `gen_dashboards.py` (the health view and nav) |
| `tests/` | the command (`tests/jev`) and the content rules (`tests/content`) |
| `docs/architecture.md` | [how the command works](docs/architecture.md) |

See [CLAUDE.md](CLAUDE.md) for the repository's conventions.

## License

Apache 2.0 ([LICENSE](LICENSE)). The vendored splunk-sdk is Apache 2.0 (see
[THIRD-PARTY-LICENSES.txt](jev_for_splunk/THIRD-PARTY-LICENSES.txt)).
