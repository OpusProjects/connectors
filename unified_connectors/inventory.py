"""The settings and credential helpers every inventory connector carries.

Inventory connectors are standalone files, so they run anywhere a single
script can: unified-api, an Ansible controller such as Ascender, a cron job.
Each one carries an identical copy of the block below instead of importing
it; CI (.github/scripts/check-helpers.sh) fails when a copy drifts from this
reference, so a fix is made here and copied to every connector.
"""

import json
import os

# --- begin shared helpers: copied from unified_connectors/inventory.py, keep identical ---


class ConnectorError(Exception):
    """A failure reported as one clean line on stderr, without a traceback."""


def settings(environ=None):
    """Read the settings unified-api hands a connector.

    Args:
        environ: The environment to read; os.environ by default.

    Returns:
        The SOURCE_CONFIG (or APPLICATION_CONFIG) JSON object, or an empty dict.

    Raises:
        ConnectorError: The variable is set but is not a JSON object.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("SOURCE_CONFIG") or environ.get("APPLICATION_CONFIG") or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConnectorError(f"SOURCE_CONFIG is not JSON: {e}") from None
    if not isinstance(value, dict):
        raise ConnectorError("SOURCE_CONFIG must be a JSON object")
    return value


def as_list(value, default=()):
    """Read a list setting: a JSON list, or a comma-separated string.

    unified-api passes every config value as a string, so "a, b" and ["a", "b"]
    both give ["a", "b"].
    """
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def as_bool(value, default=False):
    """Read a flag setting: true, 1 and yes (any case) are true, anything else false."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def as_int(value, default):
    """Read a whole-number setting.

    Raises:
        ConnectorError: The value is not a whole number.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConnectorError(f"not a whole number: {value!r}") from None


def credential_sets(prefix, required, optional=(), environ=None):
    """Find every complete set of credentials for one kind of system.

    A set is PREFIX_<TAG>_<FIELD> variables sharing a TAG, which is any name.
    unified-api hands them over as CREDENTIAL_PREFIX_<TAG>_<FIELD>; an Ansible
    controller sets PREFIX_<TAG>_<FIELD>. The unified-api form wins, so a system
    exposed both ways is used once.

    Args:
        prefix: The system's prefix, such as UCS.
        required: Fields a set must have, the first of which marks a set.
        optional: Fields read when present.
        environ: The environment to read; os.environ by default.

    Returns:
        One dict per set, sorted by tag: "tag" plus each field in lowercase.
    """
    environ = os.environ if environ is None else environ
    marker = f"_{required[0]}"
    found, seen = [], set()
    for start in (f"CREDENTIAL_{prefix}_", f"{prefix}_"):
        for key in sorted(environ):
            if not (key.startswith(start) and key.endswith(marker)):
                continue
            tag = key[len(start) : -len(marker)]
            if not tag or tag in seen:
                continue
            values = {
                f.lower(): environ.get(f"{start}{tag}_{f}", "").strip()
                for f in (*required, *optional)
            }
            if all(values[f.lower()] for f in required):
                seen.add(tag)
                found.append({"tag": tag, **values})
    return found


# --- end shared helpers ---
