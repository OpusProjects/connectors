# Security Policy

## Supported versions

Only the latest release line receives security fixes.

| Version | Supported |
|---|---|
| 0.x (latest release) | Yes |
| older tags | No |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private vulnerability reporting instead:
[Report a vulnerability](https://github.com/OpusProjects/unified-connectors/security/advisories/new)
— it opens a private thread with the maintainers.

Include what you can: the affected connector, reproduction steps, and
impact. You should hear back within a week. Once a fix ships, the advisory is
published and credited unless you prefer otherwise.

## Scope notes

- Connectors run with the privileges of the service that starts them, and with the
  credentials of the source or application that names them. Treat this repository
  like production access: whoever can change a script can change what it reads or writes.
- A connector never stores credentials. It reads them from the environment the
  service gives it, on each run, and never prints them, not even in an error.
- Writes happen only under `apply`, and only when the service sets `UNIFIED_ALLOW_WRITE=1`.
