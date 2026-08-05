# Changelog

All notable changes are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/), and versions aim to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Pinned `mcp>=1.13,<2`.** `mcp` 2.0.0 removed `mcp.server.fastmcp`, which this
  server is built on, so an unpinned install resolved 2.0.0 and died at import with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` — breaking every fresh
  clone and install, and CI along with them. `pyproject.toml` was also out of sync
  with `requirements.txt`: `mcp`/`pycrdt` unpinned there, and `markdown-it-py` /
  `mdit-py-plugins` missing entirely despite being imported at module scope.
- **`append_blocks` silently discarded content given a database row id**
  ([#1] neighbourhood). AppFlowy's REST `append-block` accepts only a real page-view;
  handed a row id it returns success and writes nothing — precisely the call used to
  fold a checklist onto a Kanban card. Rows are now re-routed through the collab
  layer, matching `add_block` / `add_blocks` / `get_page_markdown`. The page-view
  path is byte-for-byte unchanged and issues no extra request.
- **Dollar amounts were swallowed as inline math.** With dollarmath's defaults,
  `"ARR hit $560k this quarter, up from $380k last year."` parsed to
  `"ARR hit 380k last year."` — both figures and the intervening words silently gone.
  Markdown is now parsed with `allow_digits=False, allow_space=False`, which keeps
  real math (`$e^{i\pi}+1=0$`, `$$…$$`) working.
- **Ruff pinned in CI.** An unpinned linter adopts new rules on release, so a build
  that passed yesterday fails today with no code change. Pinned, and the 19 findings
  from ruff 0.16.1 cleared (12 mechanical `dict.get()` rewrites — verified safe
  against pycrdt `Map`, whose `.get()` matches `dict.get()` — and 4 deliberate
  resilience fallbacks documented with `# noqa` and a reason).

### Security
- **`?token=` is redacted from the access log.** Clients that cannot set an
  `Authorization` header pass the shared secret in the URL, and uvicorn logged the
  full request line — printing a working credential to stdout on every request,
  where `docker logs` exposes it and log shippers archive it. Observed in a deployed
  sibling server, not hypothetical. Prefer the header where the client supports it;
  redaction limits blast radius, it does not make a URL secret.

- **Pinned `pycrdt==0.13.0`.** 0.14.1 regressed collab decoding — it raised
  `Cannot decode update: while reading, an unexpected value was found` on some
  AppFlowy *database* collabs, which silently broke every Tier-2 structural op
  (`delete_row`, `update_database_field`, `delete_database_field`,
  `add_/delete_select_option`) on affected boards while row-collab cell writes kept
  working. Every pre-0.14 version (and the deployed build) decodes those collabs fine.
- **`update_row_cells` now confirms the write.** It reads the cells back from the
  authoritative collab and retries on transient contention before returning, so a
  success result means the change actually stuck (previously a racing/parallel write
  could report success without landing). New cells also take their field type from the
  DB collab instead of the lagging fields view, so a write to a just-created field is
  no longer mis-typed.

### Added
- **Page mentions in Markdown.** `[label](mention:<view_id>)` inserts a live inline
  mention (link) to another page and round-trips through `get_page_markdown`. AppFlowy's
  editor has no person-mention type, so this covers page mentions.
- **Callouts and math blocks in Markdown.** `> [!NOTE]` / `[!TIP]` / `[!WARNING]` GFM
  alerts render as AppFlowy callouts and `$$…$$` as block equations — both directions
  (create and `get_page_markdown`). Adds `mdit-py-plugins` for `$`-math parsing.
- **Agent guide as an MCP resource** (`appflowy://guide`) — the full operator guide
  (KNOWLEDGE.md) is fetchable in-protocol, so agents pull the deep reference on demand
  instead of inflating tool descriptions.
- **Markdown for pages.** `create_page` and `append_blocks` now take a `markdown=`
  argument — standard Markdown (headings; nested bulleted / numbered / `- [ ]` task
  lists; quotes; fenced code with language; dividers; images; and inline
  bold / italic / strikethrough / code / links) is parsed (via `markdown-it-py`) into
  the AppFlowy block tree. One Markdown content interface for pages and row bodies
  alike, mirroring Notion's MCP. The JSON `page_data` / `blocks` path stays for blocks
  Markdown can't express.
- **Read pages as Markdown.** `get_page_markdown` returns a Document page — or a
  database row's body — as Markdown (the inverse of `create_page(markdown=...)`), so
  agents read and write the same format (symmetric with Notion's `fetch`).
- **Rich inline formatting on in-place edits.** `add_block` and `edit_block_text` now
  render inline Markdown (bold / italic / strikethrough / code / links) into a block's
  text instead of writing it plain; code and equation blocks stay literal.
- **`replace_text`** — content-addressed find-and-replace across a document's blocks
  (no block ids), the counterpart to Notion's `update_content`.
- **Collab/CRDT layer** (via `pycrdt`): `update_row_cells`, `delete_row`,
  `add_block`, `edit_block_text`, `delete_block` — edit, move, and delete *any*
  existing row or document block (including UI-created ones), and place advanced
  blocks (callout, toggle, quote, code, …) that the REST API alone can't create.
- `add_block` / `edit_block_text` / `delete_block` accept a **database row id** for
  `page_id`, auto-resolving to the row's body document (`uuid5(row_id, "document_id")`),
  so agents can add checklist sub-tasks to a Kanban card by its row id.
- **Field/schema editing** (collab/CRDT — AppFlowy exposes no REST for it):
  `update_database_field` renames a column (or writes its raw `type_option`), and
  `delete_database_field` removes a column from the schema and every view's column
  order. The primary/title field is guarded against deletion.
- **Select-option management** (collab/CRDT): `add_select_option` (returns the new
  option id to use as a cell value; idempotent by name; strict color validation so a
  bad color can't wipe the option set) and `delete_select_option` (by option id or
  label) for SingleSelect/MultiSelect columns.
- **`set_group_by`** (collab/CRDT): sets which field a Board view groups its columns by.
  For a SingleSelect/MultiSelect it writes the same group structure the client does — a
  leading "No <field>" column then one per option — so the board re-groups immediately.
- **Structure tools**: `create_page`, `create_database` (grid/board/calendar),
  `create_database_view`, `add_database_field`, `append_blocks`, `create_space`,
  `move_page`, `duplicate_page`, `trash_page`, `restore_page`, `rename_page`
  (page/database/space), `list_databases`, `get_page`.
- **Google-federated OAuth** sign-in with an email allow-list — MCP discovery,
  dynamic client registration, and PKCE (enabled via `GOOGLE_CLIENT_ID` /
  `OAUTH_ISSUER`). Clients can connect with just the server URL.
- Optional **persistent OAuth token store** (`OAUTH_STORE_PATH`): issued tokens +
  registered clients survive restarts, so a redeploy no longer forces re-sign-in.
  Atomic `0600` file on a mounted volume; `docker-compose` ships an `oauth-data` volume.
- **Streamable HTTP** transport (alongside stdio and legacy SSE), an agent
  **knowledge pack** (server instructions + `KNOWLEDGE.md`), and a server icon.

### Changed
- Endpoint auth now accepts a Bearer header, a `?token=` link, or OAuth.
- **Tool annotations on every tool** (`readOnlyHint` / `destructiveHint` /
  `idempotentHint` / `openWorldHint`) so clients and agents can tell reads from writes
  from irreversible deletes at a glance.
- **Actionable errors.** All API calls route through one helper that turns a non-2xx into
  a specific, agent-readable message (status + what to fix + the server's own words)
  instead of a raw traceback — e.g. a 404 points you at `list_databases`.
- **General-purpose docs.** Server `instructions` and `KNOWLEDGE.md` rewritten to be
  domain-neutral (databases / documents / blocks) — recipes, best practices, and the
  pitfalls section (read-after-write lag, per-row content, id resolution) no longer assume
  any particular workflow.
- Agent knowledge pack (`KNOWLEDGE.md` + shipped `instructions`) is a full operator's
  guide: tool-by-task catalog, how-to recipes, best practices, a pitfalls / "what to
  avoid" section, and a data-model reference.

### Security
- Workspace scoping (`ALLOWED_WORKSPACE_IDS`) enforced on every tool, plus
  DNS-rebinding protection for the HTTP transports.

### Fixed
- `create_page` / `create_database` returned a dict but were annotated `-> str`,
  raising an output-validation error *after* the page was created; they now return
  the `view_id` string.

## [0.1.0]

- Initial release: workspace/folder/database reads plus row create + upsert over
  the AppFlowy Cloud REST API.
