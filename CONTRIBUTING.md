# Contributing to Unified Connectors

Thanks for helping out! This is a small project with a few firm conventions —
following them keeps the history clean and every connector predictable.

## Workflow: one logical change per PR

`main` moves only through squash-merged pull requests, one **logical change** per PR —
a new connector, a fix, a doc edit each get their own. This keeps every change
independently reviewable, bisectable, and revertable.

```bash
git checkout main && git pull
git checkout -b <type>/<short-name>       # e.g. feat/powerscale-exports
# ...make exactly one change, test it...
git commit -am "Short imperative title"
git push -u origin <type>/<short-name>
gh pr create --base main
```

### Commit / PR message style

- Title: `<type>: short imperative summary`, lowercase after the colon —
  `feat: add a connector for NFS exports`, `fix: page through every volume`.
  Types in use: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `chore`.
- Release commits are the exception: `Release x.y.z`, matching the tag.
- Body (when the change needs explaining): plain prose, *what* and *why*, wrapped
  at ~72 columns. The problem first, then the change.

## Checks

Run them locally before pushing:

```bash
ruff format --check
ruff check
pytest
```

## Connector rules

A connector follows the contract of the service that runs it:
[inventory connectors](https://github.com/OpusProjects/unified-api/blob/main/docs/connectors.md) for
unified-api, [gitops connectors](https://github.com/OpusProjects/unified-gitops/blob/main/docs/connectors.md)
for unified-gitops. On top of that:

1. **JSON on stdout, messages on stderr.** Exit 0 only with a whole document; on any
   failure, exit non-zero and say what went wrong on stderr.
2. **No secrets in the repository or the output.** Addresses and credentials come from the
   environment the service sets; never print them, not even in an error.
3. **Write only on `apply`, and only when `UNIFIED_ALLOW_WRITE=1`.** Everything else reads.
4. **Be interruptible.** A run past its timeout is killed, so never leave state that only
   a clean exit would tidy up.
5. **Standard library first.** A dependency goes in `requirements.txt` only when it earns
   its place; the services install it into the project's virtualenv.

## Code comments

Comment only when the *why* is non-obvious: a quirk of the system's API, a paging rule,
a field that means something other than its name. Comments are written in English.

## Releases

1. One release PR: move the `Unreleased` entries in `CHANGELOG.md` under the new
   version with the date.
2. After the squash merge: `git tag vX.Y.Z && git push origin vX.Y.Z`. Services pin
   a project to a tag, so a release is what reaches them.
