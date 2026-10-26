# kv

An example gitops connector: a key-value store kept as JSON files in a folder. It stands in for
a real system's API, so the whole loop runs on a laptop, and editing a file by hand shows up as
drift at the next check.

## Kinds

| Kind | Key | Depends on | Fields |
| ---- | --- | ---------- | ------ |
| `namespace` | `name` | | `description` (optional) |
| `entry` | `name` | `namespace` | `namespace`, `value`, any others |

## Settings

| Setting | Required | Meaning |
| ------- | -------- | ------- |
| `dir` | yes | The folder holding the store, one `<kind>.json` per kind |

No credentials.

## Try it

```bash
export APPLICATION_CONFIG='{"dir": "/tmp/store"}'
./kv/kv.py describe
echo '{"op":"create","object":{"name":"team-a"}}' | UNIFIED_ALLOW_WRITE=1 ./kv/kv.py apply --kind namespace
./kv/kv.py list --kind namespace
```
