---
name: factory-test-logs
base: 06fb296d8ff78d804c324fc1f1e08d12db665b7f
reference: 211dc16b7bcff60517b6110388fa782fe6ff1da1
tier: smoke
area: cli
---
Work only from commit 06fb296d8ff78d804c324fc1f1e08d12db665b7f (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`.

factory-test: make concurrent runs from different lanes stop clobbering each other.

Goal: all lanes of one role run in the same sandbox, and they share `factory-test`'s state. Today the files a run writes besides its own copy (the last-run log it tells you to grep, the install log, the yarn rc) are shared by every run, so two lanes testing at once can print each other's output or read each other's log. Give each tested directory its own set of those files, kept with its copy and removed with it, and make runs on the same directory take turns.

Files: `templates/agent/factory-test`, its test `scripts/test_factory_test.sh`.

Known facts:
- A copy lives under `$FACTORY_TEST_ROOT` (default `~/.cache/factory-test`), named from the directory and a short hash of its path; there is also a `-host` twin. Stale-copy cleanup removes a copy whose source directory is gone, and it must now remove everything kept for it, too.
- The log path that `-q` prints must still be the one to grep, and must be the run's own.
- File names (the tests look for these exact names; `<copy>` is the copy's full path, `$FACTORY_TEST_ROOT/<dir>-<12 hex of the path>`, the directory a run with no command prints), all kept directly under `$FACTORY_TEST_ROOT` beside the copy:
  - the `-q` run log is `<copy>.last-run.log`, and the `-q` line "full output: ..." prints that path; the `-host` twin's run log is `<copy>-host.last-run.log`;
  - the install log is `<copy>.install.log` (written for Python and Node installs alike);
  - the lock file is `<copy>.lock`, the yarn rc file is `<copy>.yarnrc`, and the recorded source stays `<copy>.src`;
  - the old shared `last-run.log`, `install.log` and `yarnrc` directly under `$FACTORY_TEST_ROOT` must no longer be written;
  - cleaning up a vanished directory removes every entry whose name starts with the copy's name (the copy, its `.src`, `.lock`, `.install.log`, `.yarnrc`, `.last-run.log`, and the `-host` copy and its `-host.last-run.log`), and leaves live directories' files alone.
- A run that has to wait for another run on the same copy prints exactly `factory-test: waiting for another run on this copy` on stderr. A run that does not have to wait must not print "waiting for another run".
- Runs on different directories must not wait for each other. A run that has to wait should say so. A process the test command leaves behind must not keep the lock held after factory-test exits.
- `flock` may be missing on some hosts: the test must skip the lock part there with a clear `skip -` line, not fail.
- A source directory may disappear while cleanup is running; that must not make the run fail.
- Shell rules for `scripts/`: `set -euo pipefail`, `bash -n` clean, shellcheck-clean (the repo's lint).

Done when:
- Two `factory-test` runs in two different directories at the same time each end with their own log and install log; neither prints or reads the other's.
- Two runs on the same directory run one after the other, not interleaved.
- Cleaning up a copy whose source is gone also removes the logs, lock and rc file kept for it (and the `-host` twin's log).
- The header comment of `templates/agent/factory-test` describes where these files are and the turn-taking.
Tests required: in `scripts/test_factory_test.sh`: the per-directory files (two directories, each result isolated), the turn-taking on one directory, and cleanup removing every file kept for a vanished copy. Run `bash scripts/test_factory_test.sh` and the lint.
Out of scope: factory-test's install behaviour, the Node mirror or wheels handling, `factory-lane`, other scripts.
Risk: low
