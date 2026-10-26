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
.github/scripts/check-public.sh   # backticks in Python, real-looking addresses and domains
pytest
```

## Layout

One folder per system, named `vendor_product` in lowercase with underscores (`vmware_vcenter`,
`cisco_ucs`), or one word when the vendor and the product share the name (`device42`, `centreon`).
The vendor is the brand the product is sold under; never rename a folder after an acquisition or a
product rename, because every service's config references the path.

```
vmware_vcenter/
├── vmware_vcenter.py     # the script, named like its folder
├── README.md             # what it returns, settings, credentials, permissions it needs
├── example.yaml          # a ready connector and source item for unified-api
└── tests/
    ├── test_vmware_vcenter.py
    └── fixtures/         # recorded API responses, anonymised, so tests run offline
```

`unified_connector.py` at the root is the shared SDK. Code two connectors need goes in a module at
the root too, never in a vendor folder.

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

## Docstrings and comments

Docstrings follow the Google style (`Args:`, `Returns:`, `Raises:`; ruff checks the layout) and
are plain text: no Markdown backticks, since they are read where nothing renders them. Comment
only when the *why* is non-obvious: a quirk of the system's API, a paging rule, a field that
means something other than its name. Docstrings and comments are written in English.

## Public by design

This repository is public. Nothing in it may identify where a connector runs:

- No company, team or site names, real host names, domains, addresses, serials or user names.
- Examples and fixtures use `example.com`, `example.net`, `example.org` and the documentation
  ranges `192.0.2.0/24`, `198.51.100.0/24` and `203.0.113.0/24`; CI rejects anything else.
- Fixtures keep the shape of a real response with the values rewritten and cut to a few objects.
- Settings have neutral names (`url`, `insecure`, `domain`); your real values live in your own
  configuration, never here.
- A connector ported from a private repository arrives as a new file, without its history.

## Releases

1. One release PR: move the `Unreleased` entries in `CHANGELOG.md` under the new
   version with the date.
2. After the squash merge: `git tag vX.Y.Z && git push origin vX.Y.Z`. Services pin
   a project to a tag, so a release is what reaches them.
