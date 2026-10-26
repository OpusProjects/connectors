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
| `user` | `Username` | A user who may sign in, with `Role` `administrator` or `standard`; after `settings` |

- **settings** are updated with `PUT /api/settings`; they cannot be created or deleted.
- **environment** updates lay each declared block over the live one, because `PUT` replaces a
  whole block (Kubernetes snapshots included). A new agent needs `Type`, `URL`, and optionally
  `ContainerEngine` and `TLSConfig.TLSSkipVerify`; Portainer dials the agent while creating it.
  The local environment Portainer runs in is never created or deleted here.
- **user** is created with no password, for LDAP sign-in. The built-in admin (user 1) is never
  listed or touched: it is the way back in.

One application can hold all three kinds. With prune on, users not in git are deleted (an
allow-list), and so are agent environments; Portainer's local environment never is. To keep
environments out of prune, put users in an application of their own.

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
| `ldap_bind_password` | Optional: the LDAP bind password, sent with settings that declare `LDAPSettings` |

Portainer never returns the bind password, so git never holds it. Portainer keeps the stored one
when an update carries none; with `ldap_bind_password` set, the connector adds it to every settings
update that carries `LDAPSettings`, so a rotated password arrives with the next one.

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

See [example.yaml](example.yaml) for the connector, credentials and the application.
