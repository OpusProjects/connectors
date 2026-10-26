# unified_connectors

The shared SDK every connector in this repository imports. Standard library only, Python 3.11+.

```python
# The SDK, the unified_connectors package, sits at the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connectors import (
    ConnectorError,
    as_bool,
    as_list,
    credential_sets,
    settings,
)
```

## Inventory connectors (unified-api)

| Helper | What it does |
| ------ | ------------ |
| `settings()` | The source's `config:` as a dict, from `SOURCE_CONFIG`; refuses anything but a JSON object |
| `as_list(value, default)` | A list from a comma-separated string (`"a, b"`) or a JSON list |
| `as_bool(value, default)` | `true`, `1` and `yes` in any case are true |
| `as_int(value, default)` | A whole number, or a clear error |
| `credential_sets(prefix, required, optional)` | Every complete `<PREFIX>_<TAG>_<FIELD>` set, unified-api's `CREDENTIAL_` form first |

unified-api passes every `config:` value as a string, so a connector reads `domains: "a,b"` with
`as_list` and `insecure: "true"` with `as_bool`.

## Gitops connectors (unified-gitops)

A gitops connector declares the kinds it manages and hands two functions to `run`:

```python
run(
    kinds={
        "namespace": {"key": "name"},
        "entry": {"key": "name", "depends_on": ["namespace"]},
    },
    list_objects=list_objects,  # (kind, config, credentials) -> the objects that exist now
    apply=apply,  # (kind, op, obj, config, credentials) -> create, update or delete one
)
```

`run` answers `describe`, `list --kind K` and `apply --kind K`, reads `APPLICATION_CONFIG` and
the `CREDENTIAL_*` variables, refuses to write unless `UNIFIED_ALLOW_WRITE=1`, and prints JSON on
stdout with exit 0 only after a whole answer. `kv/` is a complete example.

## Errors

Raise `ConnectorError("...")` for a failure worth one clean line on stderr; the connector exits 1.
