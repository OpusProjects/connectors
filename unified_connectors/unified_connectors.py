"""The connector side of the unified-gitops contract, in one file.

A gitops connector describes the kinds of object it manages, writes two
functions and hands them to run:

    from unified_connectors import ConnectorError, run

    def list_objects(kind, config, credentials):
        return [{"name": "a"}]                    # what exists now

    def apply(kind, op, obj, config, credentials):
        ...                                       # create, update or delete one object

    run(
        kinds={
            "namespace": {"key": "name"},
            "entry": {"key": "name", "depends_on": ["namespace"]},
        },
        list_objects=list_objects,
        apply=apply,
    )

The kinds live with the code that handles them, not in each instance's
configuration. For each type of object they name the field that identifies one
(key, matched between git and the system) and the types that must exist first
(depends_on: applied before it, deleted after). The describe command hands them
to unified-gitops at the start of every run.

run handles everything else, the same way for every connector:

- the commands: describe, list --kind K and apply --kind K
- the APPLICATION_CONFIG JSON as config, and CREDENTIAL_<FIELD> as credentials
- the write guard: apply refuses to run unless UNIFIED_ALLOW_WRITE=1, which only
  unified-gitops sets, so a script shared with unified-api cannot write by mistake
- JSON on stdout, messages on stderr, and exit 0 only after a whole document

Standard library only, so it runs on any Python 3.11+ without a virtualenv.
"""

import json
import os
import sys
import traceback

PROTOCOL = 1


class ConnectorError(Exception):
    """A failure reported as one clean line on stderr, without a traceback."""


def _credentials():
    prefix = "CREDENTIAL_"
    return {
        k[len(prefix) :].lower(): v
        for k, v in os.environ.items()
        if k.startswith(prefix)
    }


def _config():
    raw = (
        os.environ.get("APPLICATION_CONFIG") or os.environ.get("SOURCE_CONFIG") or "{}"
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConnectorError(f"APPLICATION_CONFIG is not JSON: {e}") from None
    if not isinstance(value, dict):
        raise ConnectorError("APPLICATION_CONFIG must be a JSON object")
    return value


def _kind(args, kinds):
    if len(args) != 2 or args[0] != "--kind":
        raise ConnectorError("expected --kind <kind>")
    kind = args[1]
    if kind not in kinds:
        raise ConnectorError(
            f"unknown kind '{kind}' (this connector manages: {', '.join(kinds)})"
        )
    return kind


def _describe(kinds):
    """Build the describe answer from the kinds a connector declares.

    Args:
        kinds: Each kind's name mapped to its key field and optional depends_on.

    Returns:
        One dict per kind with its kind, key and depends_on.

    Raises:
        ConnectorError: A kind declares no key.
    """
    out = []
    for name, spec in kinds.items():
        if not isinstance(spec, dict) or not spec.get("key"):
            raise ConnectorError(
                f"kind '{name}' needs a 'key': the field that identifies an object"
            )
        out.append(
            {
                "kind": name,
                "key": spec["key"],
                "depends_on": list(spec.get("depends_on", [])),
            }
        )
    return out


def run(kinds, list_objects, apply):
    """Run the command named on the command line, print its result and exit.

    Args:
        kinds: Each kind's name mapped to its key field and optional depends_on.
        list_objects: Called as list_objects(kind, config, credentials); returns
            the objects of that kind that exist on the system now.
        apply: Called as apply(kind, op, obj, config, credentials) to create,
            update or delete one object.
    """
    try:
        if len(sys.argv) < 2:
            raise ConnectorError("usage: describe | list --kind K | apply --kind K")
        command, args = sys.argv[1], sys.argv[2:]
        if command == "describe":
            print(json.dumps({"protocol": PROTOCOL, "kinds": _describe(kinds)}))
        elif command == "list":
            kind = _kind(args, kinds)
            objects = list_objects(kind, _config(), _credentials())
            if not isinstance(objects, list):
                raise ConnectorError("list_objects must return a list")
            print(json.dumps(objects))
        elif command == "apply":
            kind = _kind(args, kinds)
            if os.environ.get("UNIFIED_ALLOW_WRITE") != "1":
                raise ConnectorError(
                    "refusing to write: UNIFIED_ALLOW_WRITE is not set"
                )
            request = json.load(sys.stdin)
            op, obj = request.get("op"), request.get("object")
            if op not in ("create", "update", "delete") or not isinstance(obj, dict):
                raise ConnectorError(
                    'stdin must be {"op": create|update|delete, "object": {...}}'
                )
            apply(kind, op, obj, _config(), _credentials())
        else:
            raise ConnectorError(f"unknown command '{command}'")
    except ConnectorError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 — every failure must exit non-zero
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
