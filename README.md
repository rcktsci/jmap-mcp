# JMAP MCP Server

MCP server for a [Stalwart](https://stalwartlabs.com/) mail server: a stateless, credential-transparent
proxy that speaks **MCP on the outside and JMAP on the inside**.

The client sends its own Stalwart API key in the `Authorization` header of every MCP request. The server
forwards that header to the mail server as it is and stores nothing: no database, no user table, no key
registry, no health endpoint, no UI. Fifteen tools cover reading, changing and sending mail.

## Features

- Bearer pass-through: the key of the caller is the only credential, and it lives exactly as long as the
  request that carried it (a context variable, never a file)
- Fixed upstream: the JMAP session document is checked against `stalwart.url` before a call is made
- Fifteen tools: read mail, change flags, move messages, create and send drafts, up/download attachments
- File sandbox: `upload_attachment` reads only from the allowed roots, `download_attachment` writes only
  into the download directory
- Transport: stateless Streamable HTTP
- No database, no ORM, no web framework of its own - the server is a proxy and nothing else

## Tools

| Tool                   | Purpose                                                      | JMAP                      |
|------------------------|--------------------------------------------------------------|---------------------------|
| `whoami`               | The account behind the key: id, addresses, capabilities      | session + `Identity/get`  |
| `list_identities`      | The sender addresses (aliases) of the account                | `Identity/get`            |
| `list_mailboxes`       | The folders: role, parent, counters - the cheapest tool      | `Mailbox/get`             |
| `list_emails`          | Messages of a folder as summaries, no bodies                 | `Email/query` + `Email/get` |
| `search_emails`        | Search by text, header, folder, date and flags               | `Email/query` + `Email/get` |
| `count_emails`         | Only the number of messages under a filter                   | `Email/query`             |
| `get_email`            | One message: headers, plain-text body, attachments           | `Email/get`               |
| `get_thread`           | A conversation: one summary per message, bodies on demand    | `Thread/get` + `Email/get` |
| `set_flags`            | Read/unread, star, replied - by id, 1 to 100 per call        | `Email/set` (keywords)    |
| `move_emails`          | Move messages to another folder, by role or by id            | `Email/set` (mailboxIds)  |
| `create_draft`         | A draft in the Drafts folder - it sends nothing              | `Email/set` (create)      |
| `send_draft`           | Send an existing draft, Drafts → Sent                        | `EmailSubmission/set`     |
| `list_changes`         | What changed since a state token (ids only)                  | `Email/changes`           |
| `download_attachment`  | Download an attachment to a file on the server               | blob GET (`downloadUrl`)  |
| `upload_attachment`    | Upload a file of the server, get a `blob_id` for a draft     | blob POST (`uploadUrl`)   |

The full contract of every tool (input schema, output schema, errors) lives in
[`docs/tools-schema.md`](docs/tools-schema.md).

## Security model

- **The key is not stored.** It is read from the `Authorization` header of the request being served and
  lives in a context variable until the answer is sent. Nothing is written to disk, and the key never
  appears in a log line or in an error message - for the correlation of log lines only a short
  non-reversible fingerprint (`sha256` prefix) is used.
- **The validity of the key is decided by Stalwart, not by the server.** A bad key comes back as the 401
  of the mail server. The server does not keep a registry of keys.
- **The upstream is fixed.** `apiUrl`, `downloadUrl` and `uploadUrl` come from the session document, but
  scheme, host and port are compared with `stalwart.url` before the call - a session document that points
  somewhere else is refused instead of leaking the key to another host.
- **No authentication of its own.** Everybody who reaches the port uses their own key against your mail
  server, so the server listens on `127.0.0.1` by default. For an external access put a reverse proxy with
  an authentication of its own in front of it.
- **File sandbox.** `upload_attachment` reads a file only when its resolved path (symlinks included) lies
  inside one of `allow_roots`; `download_attachment` writes only into `download_dir`, under a sanitized
  file name that never overwrites an existing file.
- **Limits.** The size of an attachment (`max_attachment_mb`, 25 MB by default), the size of a page
  (`max_page_size`) and the size of a body (`max_body_chars`) are capped.

## Configuration

`config.yml` (copy `config.yml.example`, no secrets in it) or environment variables - the environment
wins. See [`.env.example`](.env.example) for the environment names.

```yaml
stalwart:
  url: "https://mail.example.com"   # the only upstream
  session_path: "/jmap/session"      # fallback: /.well-known/jmap
  request_timeout_sec: 30
  max_attachment_mb: 25
  max_page_size: 50
  max_body_chars: 20000
  allow_roots: ["."]                 # where upload_attachment may read
  download_dir: null                 # where download_attachment writes (null = a temp directory)
  session_cache_ttl_sec: 60
listen: "127.0.0.1"
port: 8000
log_level: "info"
```

| Variable               | Meaning                                            | Default        |
|------------------------|----------------------------------------------------|----------------|
| `STALWART_URL`         | Base URL of the mail server                        | `http://127.0.0.1:8080` |
| `STALWART_SESSION_PATH`| Path of the JMAP session document                  | `/jmap/session` |
| `REQUEST_TIMEOUT_SEC`  | Timeout of one upstream request                    | `30`           |
| `MAX_ATTACHMENT_MB`    | Biggest attachment that may be up/downloaded       | `25`           |
| `MAX_PAGE_SIZE`        | Biggest page of messages one tool may return       | `50`           |
| `MAX_BODY_CHARS`       | Biggest body of one message a tool may return      | `20000`        |
| `ALLOW_ROOTS`          | Comma separated roots `upload_attachment` may read | `.`            |
| `DOWNLOAD_DIR`         | Where `download_attachment` writes                 | a temp directory |
| `SESSION_CACHE_TTL_SEC`| How long a session document is kept in memory      | `60`           |
| `HOST`, `PORT`         | Address of the MCP server                          | `127.0.0.1:8000` |
| `LOG_LEVEL`            | `debug`, `info`, `warning`, `error`                | `info`         |

## Running

Python 3.14:

```sh
pip install -r requirements.txt
python main.py
```

Docker:

```sh
docker compose up -d
```

The image is published from `main` on every push as `ghcr.io/rcktsci/jmap-mcp:<date>`, e.g.:

```sh
docker pull ghcr.io/rcktsci/jmap-mcp:2026-10-27
```

The MCP endpoint is `http://<host>:8000/mcp` (Streamable HTTP). Every request needs a bearer:

```sh
curl -H "Authorization: Bearer $STALWART_API_KEY" http://127.0.0.1:8000/mcp
```

## Connecting to OpenCode

The config format is **V2** (`mcp.servers.<name>`, `type: "remote"`, `oauth: false`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "jmap": {
        "type": "remote",
        "url": "http://127.0.0.1:8000/mcp",
        "oauth": false,
        "headers": {
          "Authorization": "Bearer {env:STALWART_API_KEY}"
        },
        "enabled": true
      }
    }
  }
}
```

`STALWART_API_KEY` is the API key of the mail account - the server never asks for it and never stores it.

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest -q          # the tests
python tools/tools_schema.py # regenerates docs/tools-schema.md and checks it
```
