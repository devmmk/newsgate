# NewsGate

NewsGate pulls recent Telegram channel posts, publishes them over DNS, and lets lightweight clients reconstruct the feed without relying on a traditional HTTP backend.

It supports two server-side delivery modes:

- `cloudflare`: publish compact TXT snapshots into Cloudflare DNS records
- `authoritative_dns`: serve the same TXT snapshots directly from an authoritative DNS server

## Architecture

```text
+----------------+        +---------------------+        +----------------+
| Telegram       | -----> | NewsGate server     | -----> | DNS transport  |
| channels       |        | Telethon + packer   |        | Cloudflare or  |
|                |        |                     |        | authoritative  |
+----------------+        +---------------------+        +----------------+
                                                               |
                                                               v
                                                      +----------------+
                                                      | NewsGate client|
                                                      | DNS fetch + UI |
                                                      +----------------+
```

Server snapshots are encoded as compact JSON TXT payloads with:

- stable Telegram message IDs
- message revision hashes for edit detection
- exact message text, including multiline bodies and media captions
- part metadata for multi-record snapshots

The client deduplicates by message ID, applies edits in place, and renders messages as Telegram sent them.

## Features

- Telethon-based channel ingestion
- byte-safe TXT packing for UTF-8 text, Persian text, and emoji
- optional multi-record payload splitting
- Cloudflare TXT publishing mode
- authoritative DNS serving mode
- resolver-based client fetching
- lightweight built-in web UI and JSON endpoint

## Setup

1. Fill `server/config.yaml` and `client/config.yaml`.
2. Generate the Telethon session:

```bash
cd server
./generate_session.sh
```

For Docker:

```bash
cd server
./generate_session.sh --docker
```

## Running

### Local

Server:

```bash
cd server
python server.py
```

Client:

```bash
cd client
python client.py --api
```

### Docker

Server:

```bash
cd server
docker compose up --build
```

Client:

Run locally or containerize separately as needed.

## Delivery Modes

### Cloudflare mode

Use this when you want NewsGate to update TXT records inside a Cloudflare-managed zone.

Notes:

- `record_name` is the base TXT record, for example `news.example.com`
- extra records are derived automatically, for example `news-1.example.com`
- the client must use the same base DNS name and `extra_records` count

### Authoritative DNS mode

Use this when you want NewsGate itself to answer TXT queries as an authoritative DNS server.

Notes:

- `authoritative_dns.domain` is the DNS zone this server is authoritative for
- `record_name` and derived extra record names must live under that zone or be otherwise routed to this DNS server
- Docker exposes `5353/tcp` and `5353/udp` by default; if you change the port in config, update `server/docker-compose.yml`
- for public delegation you usually need NS records and, if required, glue records at your parent DNS provider

## Configuration

| config | default | detail |
| --- | --- | --- |
| `server.mode` | `cloudflare` | Delivery mode: `cloudflare` or `authoritative_dns`. |
| `server.update_interval_sec` | `60` | Polling and publish interval in seconds. |
| `server.max_messages` | `30` | Recent messages per channel to fetch. |
| `server.max_txt_len` | `1800` | Maximum payload size per TXT record in bytes, capped internally to a safe DNS budget. |
| `server.send_media` | `false` | Enable single-image extraction (no albums, videos are caption-only). |
| `server.session_name` | `newsgate` | Telethon session name. |
| `server.telegram.source_channels` | `[]` | List of source channels. |
| `server.cloudflare.record_name` | `news.example.com` | Base TXT record name. |
| `server.cloudflare.extra_records` | `0` | Number of extra TXT records. |
| `server.cloudflare.ttl` | `120` | TXT record TTL. |
| `server.cloudflare.proxied` | `false` | Must remain `false` for TXT records. |
| `server.authoritative_dns.domain` | `news.example.com` | Authoritative zone. |
| `server.authoritative_dns.host` | `0.0.0.0` | Bind address. |
| `server.authoritative_dns.port` | `5353` | Bind port. |
| `server.authoritative_dns.ttl` | `30` | TXT record TTL. |
| `server.authoritative_dns.ns_host` | `ns1.news.example.com` | NS host for SOA/NS responses. |
| `server.authoritative_dns.soa_email` | `hostmaster.news.example.com` | SOA email. |
| `server.authoritative_dns.tcp` | `true` | Enable TCP listener. |
| `client.update_interval_sec` | `30` | Client refresh interval in seconds. |
| `client.dns_name` | `news.example.com` | Base DNS name to query. |
| `client.extra_records` | `0` | Must match the server. |
| `client.resolvers_file` | `resolvers.txt` | Resolver list file. |
| `client.ok_resolvers_file` | `data/ok_resolvers.txt` | Cache of working resolvers. |
| `client.data_file` | `data/messages.sqlite3` | Local SQLite store path. |
| `client.timeout_sec` | `2.0` | Resolver timeout in seconds. |
| `client.max_parallel` | `25` | Parallel resolver probes. |
| `client.api.enabled` | `true` | Enable the API/UI server. |
| `client.api.host` | `0.0.0.0` | API/UI bind host. |
| `client.api.port` | `8080` | API/UI bind port. |
| `client.api.path` | `/news` | Base path for the web UI. |
| `client.api.allow_cors` | `false` | Allow CORS for the API/UI. |

If `client.api.path` is `/news`:

- UI: `http://HOST:PORT/news`
- JSON: `http://HOST:PORT/news/data`

## Operational Notes

- Media captions are included because message extraction uses Telegram raw text.
- DNS payloads are zlib-compressed and base64-wrapped before publishing.
- Messages that do not fit into the configured TXT budget are skipped rather than truncated.
- The client accepts only complete metadata batches and keeps edited messages up to date by revision hash.
- In authoritative mode, the DNS resolver is the transport path. It is not the source of truth; Telegram remains the source.

## Support

If this project helps you, please consider starring the repository and sharing it.

[![Telegram](https://img.shields.io/badge/Telegram-Channel-229ED9?logo=telegram&logoColor=white)](https://t.me/mat_log)

### Donate

- Tether (ERC20): `0xCEaAa7b52945A08AEb26B606B68325A824C25BeB`
- Tether (BEP20): `0xCEaAa7b52945A08AEb26B606B68325A824C25BeB`
