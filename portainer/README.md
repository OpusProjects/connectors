# portainer

A gitops connector for Portainer: its global settings, its environments and the users who may sign
in. Portainer keeps all three in its database, behind its API; this connector is the layer between
that API and the YAML in git.

Field names are Portainer's own, as `GET` returns them, so a desired-state file reads against the
API reference. Only the fields a file declares are compared and written; the rest stay as they are.

## Kinds

| Kind | Key | What it is |
| ---- | --- | ---------- |
| `settings` | `name` | The global settings, one object whose `name` is `settings` |
| `environment` | `Name` | An environment; created only for agents (`Type` 2 or 6) |
| `user` | `Username` | A user who may sign in, with `Role` `administrator` or `standard` |

- **settings** are updated with `PUT /api/settings`; they cannot be created or deleted.
- **environment** updates lay each declared block over the live one, because `PUT` replaces a
  whole block (Kubernetes snapshots included). A new agent needs `Type`, `URL`, and optionally
  `ContainerEngine` and `TLSConfig.TLSSkipVerify`; Portainer dials the agent while creating it.
  The local environment Portainer runs in is never created or deleted here.
- **user** is created with no password, for LDAP sign-in. The built-in admin (user 1) is never
  listed or touched: it is the way back in.

Prune is per application, so keep users in an application of their own when they are an allow-list
(prune on), and settings and environments in another (prune off).

## Settings

| Setting | Required | Meaning |
| ------- | -------- | ------- |
| `url` | yes | Portainer's `https://` URL, as its certificate names it |
| `address` | no | Where to connect instead; the URL's name is still used for TLS and `Host` |
| `insecure` | no | `true` to skip certificate verification |

## Credentials

| Field | Meaning |
| ----- | ------- |
| `api_key` | A Portainer access token (or `username` and `password` instead) |
| `ldap_bind_password` | The LDAP bind password, needed when `settings` declare `LDAPSettings` |

Portainer never returns the bind password, so git never holds it: the connector adds it to every
settings update that carries `LDAPSettings`, which would otherwise blank it. A rotated password
reaches Portainer with the next settings update.

## Desired state

```yaml
# settings.yaml
kind: settings
items:
  - name: settings
    AuthenticationMethod: 2
    LDAPSettings:
      URL: ldap.example.com:636
      ReaderDN: svc-reader@example.com
---
# environments.yaml (a separate file: one kind per file)
kind: environment
items:
  - Name: agent-a
    Type: 2
    URL: tcp://agent-a.example.com:9001
    ContainerEngine: podman
---
# users.yaml
kind: user
items:
  - Username: jane.doe
    Role: administrator
```

See [example.yaml](example.yaml) for the connector, credentials and the two applications.
