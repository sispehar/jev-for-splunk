# Releasing

A release is a GitHub release with the built package attached. GitHub Actions publishes it when a `v*`
tag is pushed (`.github/workflows/ci.yml`). The app is not on Splunkbase: the Splunkbase app with the id
`jev_for_splunk` is a different app, which is why `check_for_updates` stays `false`.

1. Add the changes at the top of `jev_for_splunk/RELEASE_NOTES.md`, under a `## <version>` heading. That
   entry becomes the release's "What's in" section.
2. Bump the version and build:
   ```bash
   ./scripts/build.sh --release patch --appinspect    # 0.1.1 -> 0.1.2; "minor" for 0.2.0
   ```
   The build runs the tests on Python 3.9 and 3.13 and AppInspect with the `cloud` tags, and stops on any
   failure. These warnings are expected (report: `dist/appinspect-jev_for_splunk.json`):

   | check | why it stays |
   |---|---|
   | `check_python_sdk_version` | splunk-sdk 3.x needs Python 3.13; Splunk 10.0 and 10.1 have only 3.9 |
   | `check_for_splunk_js` | telemetry only; the Setup page uses SplunkJS |
   | `check_collections_conf` | informational (the `jev_cache` collection) |
   | `check_for_python_script_existence` | informational |

   A `future_failure` above 0 means a coming AppInspect release will fail the app.
3. Install `dist/jev_for_splunk.tar.gz` on a test search head (Install app from file, *Upgrade app*
   ticked, then restart), run the self-test on the Setup page and the searches in
   [Check the install](../README.md#check-the-install).
4. Commit and push, then tag the commit and push the tag:
   ```bash
   git tag v0.1.2 && git push origin v0.1.2
   ```
   CI builds the package with `--keep-build`, so it carries the committed version and build, checks that
   the tag matches `app.conf`, and publishes `jev_for_splunk-0.1.2.tar.gz` with notes from
   `scripts/release_notes.py`: install steps, what's new, the AppInspect result and the SHA-256.
