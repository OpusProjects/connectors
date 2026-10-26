#!/usr/bin/env python3
"""Example connector: a key-value store kept as JSON files in a folder.

It stands in for a real system's API so the whole gitops loop can run on a
laptop. The application's config names the folder (the dir setting), and each
kind is one file of objects keyed by name. Editing a file by hand is "someone
changed the system outside git", which the next check reports as drift.

Kinds, described to unified-gitops by the describe command (see KINDS):
  namespace  {"name", "description"?}
  entry      {"name", "namespace", "value", ...}, depends on namespace
"""

import json
import os
import sys

# The SDK sits at the repository root, one folder up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connector import ConnectorError, run

# What this connector manages: each type of object, the field that identifies
# one, and what must exist first. Entries live in a namespace, so namespaces
# are created before entries and deleted after them.
KINDS = {
    "namespace": {"key": "name"},
    "entry": {"key": "name", "depends_on": ["namespace"]},
}


def _file(config, kind):
    """Return the path of one kind's file, creating the store folder if needed."""
    folder = config.get("dir")
    if not folder:
        raise ConnectorError(
            "application config needs 'dir': the folder the store lives in"
        )
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{kind}.json")


def _load(config, kind):
    """Read one kind's objects, keyed by name; an absent file is an empty store."""
    path = _file(config, kind)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save(config, kind, data):
    """Write one kind's objects via a temporary file, so a kill never truncates it."""
    path = _file(config, kind)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def list_objects(kind, config, credentials):
    """Return the objects of one kind, sorted by name.

    Sorting is what a real connector should do too: the order of the system's
    answer never looks like a change.

    Args:
        kind: namespace or entry.
        config: The application's settings; dir is the store folder.
        credentials: Unused; the store needs none.

    Returns:
        The objects of that kind.
    """
    data = _load(config, kind)
    return [data[name] for name in sorted(data)]


def apply(kind, op, obj, config, credentials):
    """Create, update or delete one object.

    Args:
        kind: namespace or entry.
        op: create, update or delete.
        obj: The object as git declares it, or as list returned it for a delete.
        config: The application's settings; dir is the store folder.
        credentials: Unused; the store needs none.

    Raises:
        ConnectorError: An entry names a namespace that does not exist.
    """
    data = _load(config, kind)
    name = obj["name"]
    if kind == "entry" and op != "delete":
        namespaces = _load(config, "namespace")
        if obj.get("namespace") not in namespaces:
            raise ConnectorError(
                f"entry '{name}': namespace '{obj.get('namespace')}' does not exist"
            )
    if op == "delete":
        data.pop(name, None)
    elif op == "create":
        data[name] = obj
    else:
        # update: only the declared fields change; the rest of what the
        # system holds is left alone, as a PATCH to a real API would.
        data[name] = {**data.get(name, {}), **obj}
    _save(config, kind, data)
    print(f"{op} {kind} '{name}'", file=sys.stderr)


run(kinds=KINDS, list_objects=list_objects, apply=apply)
