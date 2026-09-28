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

A question can replace a pile of regexes. This one finds the Bash commands Claude Code ran with a secret
on the command line, such as a password passed to `plink -pw`, without a pattern for each tool:

```
index=anthropic sourcetype=anthropic:api tool_name=Bash
| jev noul tool_parameters "The command contains a password token or API key typed in plain text"
| where jev_noul > 0.9
| table _time jev_noul jev_noul_tokens tool_parameters
```

Answers are cached in the KV store, one per model, question and state (the fields you name), without
the event text. Asking again, or asking the same question from a dashboard, only pays for events that
were never judged. The full command reference, including batteries (several questions in one request),
is in [jev_for_splunk/README.md](jev_for_splunk/README.md).

## Install

Requirements: Splunk Enterprise 9.3 or later (the command runs on Python 3.9, and on 3.13 from 10.2), the
KV store enabled on the search head, outbound HTTPS from the search head to `api.typesafe.ai`, and a
TypeSafe API key ([console.typesafe.ai](https://console.typesafe.ai)).

1. Download `jev_for_splunk-<version>.tar.gz` from the
   [latest release](https://github.com/sispehar/jev-for-splunk/releases/latest) and install it on the
   search head (**Apps > Manage Apps > Install app from file**), then restart Splunk. Indexers and
   forwarders don't need it. To build the package from source instead:
   ```bash
   ./scripts/build.sh --appinspect     # dist/jev_for_splunk.tar.gz
   ```
2. Open **Jev for Splunk > Setup**, paste the TypeSafe key and run the self-test.
3. Give the people who run `| jev` the **jev_user** role. They don't need `list_storage_passwords`; see
   [Setup and permissions](jev_for_splunk/README.md#setup-and-permissions).

`| jev` runs only on the search head, where the key, the cache and the outbound route are. It sends only
the fields a search names, and the question, to `api.typesafe.ai`, billed to your TypeSafe account. No
event data leaves Splunk until someone runs `| jev`. See TypeSafe's
[Privacy Policy](https://typesafe.ai/legal/privacy-policy) and
[Data Processing Agreement](https://typesafe.ai/legal/data-processing).

This app is not on Splunkbase. *Jev for Splunk (unofficial)* on Splunkbase is a different app with the same
folder name, so installing it on a search head replaces this one, and the reverse.

## Check the install

| check | SPL | expected |
|---|---|---|
| self-test | `\| jevtest live=true` | all ok, including `kvstore` and `command_owner=jev_for_splunk` |
| a question | `\| makeresults \| eval message="My 20% code was accepted but I paid full price" \| jev noul message "The customer says they were charged more than they should have been" as overcharged` | `overcharged` above 0.8; run it again: `overcharged_cached=1` |
| `=` in a question | `\| makeresults \| eval message="x" \| jev noul message "Is 2+2=4 stated?" as eq` | no "Unrecognized option" |

The **Health** view shows the self-test, fresh answers against cached ones, cost by app and user, which
fields each search sent to TypeSafe, and warnings from `jev.log`.

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

GitHub Actions runs the same build (tests, AppInspect, the package as an artifact) on every push and pull
request, and a `v*` tag publishes the release ([docs/releasing.md](docs/releasing.md)).

| path | what it is |
|---|---|
| `jev_for_splunk/` | the Splunk app: `\| jev` (noul, choice, score and batteries), a KV store judgment cache, a setup page, `\| jevtest`, `\| jevpurge`, a health view |
| `jev_for_splunk/bin/jev_core/` | the logic, standard library only, tested without Splunk |
| `scripts/` | `build.sh`, `gen_dashboards.py` (the health view and nav), `gen_icons.py` (the app icons) and `release_notes.py` |
| `tests/` | the command (`tests/jev`) and the content rules (`tests/content`) |
| `docs/architecture.md` | [how the command works](docs/architecture.md) |
| `docs/releasing.md` | [how to cut a GitHub release](docs/releasing.md) |

See [CLAUDE.md](CLAUDE.md) for the repository's conventions.

## License

Apache 2.0 ([LICENSE](LICENSE)). The vendored splunk-sdk is Apache 2.0 (see
[THIRD-PARTY-LICENSES.txt](jev_for_splunk/THIRD-PARTY-LICENSES.txt)).
