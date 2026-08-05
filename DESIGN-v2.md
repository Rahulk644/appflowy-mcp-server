# v2 design: 35 tools → 12 verbs

Status: **approved, not yet implemented.** Branch `v2/consolidated-async-surface`.

This is the implementation contract for v2.0.0. It exists so the rewrite is mechanical
rather than a fresh research problem, and so the API findings below are not re-derived.

---

## Why

The v1 surface grew one tool per endpoint to 35 tools. Measured against the MCP
best-practice checklist, v1 fails on six counts:

| Check | v1 |
|---|---|
| Server name `{service}_mcp` | ✗ `appflowy-mcp` (hyphen) |
| Service prefix on tool names | ✗ 0 of 35 — bare `create_page`, `get_page` |
| Async network I/O | ✗ fully sync (`httpx.Client`, 0 async tools) |
| Pydantic input models | ✗ none |
| Pagination (`limit`/`has_more`/`next_offset`) | ✗ none |
| `response_format` (markdown/json) | ✗ none |
| Tool annotations | ✓ (missing `title`) |
| Actionable errors | ✓ |
| Guide as MCP resource | ✓ |

Two matter more than the tool count:

**Sync I/O.** Every AppFlowy call blocks the event loop, so concurrent agent calls
serialise. All network operations must be `async`.

**No prefixes.** Bare `search` / `get_page` collide when this server runs alongside
another MCP — which is the normal case.

On tool count: a large surface forces the agent to load and choose among dozens of
schemas on every call. Notion and Atlassian get comprehensive coverage *and* a small
surface by command-multiplexing writes and keeping one passthrough. That is the model.

## Principles

1. Few general verbs over the whole workspace, addressed by id.
2. Writes multiplexed behind one `command` enum per resource family.
3. A universal `fetch(id)`, with `id="self"` for identity/context.
4. Markdown in and out — never block JSON or CRDT internals.
5. Batch by default (`pages[]`, `rows[]`) — one round-trip, not N.
6. Explicit response-size controls: `limit`, `offset`, `response_format`.
7. Deep reference in the `appflowy://guide` MCP resource, not tool descriptions.
8. One escape hatch (`appflowy_api`) so a small surface never means reduced coverage.

## The 12 verbs

| Verb | Absorbs (v1) |
|---|---|
| `appflowy_search` | *new* |
| `appflowy_fetch` | `get_workspaces`, `get_workspace_folder`, `get_page`, `get_page_markdown`, `get_database_fields` |
| `appflowy_create_pages` | `create_page`, `create_space`, `create_database`, `create_database_view` |
| `appflowy_update` | `rename_page`, `move_page`, `duplicate_page`, `trash_page`, `restore_page`, `append_blocks` |
| `appflowy_delete` | `trash_page` (hard), `delete_row`, `delete_block`, `delete_database_field` |
| `appflowy_database` | `list_databases`, `add_database_field`, `update_database_field`, `delete_database_field`, `add_select_option`, `delete_select_option`, `set_group_by` |
| `appflowy_rows` | `get_database_row_ids`, `get_database_row_details`, `create_database_row`, `upsert_database_row`, `update_row_cells`, `delete_row`, `list_updated_rows` |
| `appflowy_blocks` | `add_block`, `add_blocks`, `edit_block_text`, `delete_block`, `replace_text` |
| `appflowy_ai` | *new* |
| `appflowy_import` | *new* |
| `appflowy_export` | *new* |
| `appflowy_api` | escape hatch |

Command enums:

- `appflowy_update`: `rename | move | duplicate | trash | restore | append | prepend | replace`
- `appflowy_database`: `list | fields | add_field | update_field | delete_field | add_option | delete_option | group_by`
- `appflowy_rows`: `list | get | create | upsert | update_cells | delete | changed_since`
- `appflowy_blocks`: `add | edit | delete | replace_text`
- `appflowy_ai`: `create_chat | ask | messages | delete_chat | set_context`

---

## API findings (verified against AppFlowy-Cloud source — do not re-research)

### AI chat — a real REST surface, no CRDT

From `src/api/chat.rs`:

| Method | Path |
|---|---|
| POST | `/api/chat/{workspace_id}` — create chat (`CreateChatParams`) |
| DELETE | `/api/chat/{workspace_id}/{chat_id}` |
| GET | `/api/chat/{workspace_id}/{chat_id}` — list messages (cursor pagination) |
| POST | `/api/chat/{workspace_id}/{chat_id}/message/question` — ask (`CreateChatMessageParams`) |
| GET | `/api/chat/{workspace_id}/{chat_id}/{message_id}/answer` — **non-streaming answer** |
| POST | `/api/chat/{workspace_id}/{chat_id}/answer/stream` — SSE stream |
| GET/POST | `/api/chat/{workspace_id}/{chat_id}/settings` — holds **`rag_ids`** |
| POST | `/api/chat/{workspace_id}/{chat_id}/context/text` |
| GET | `/api/chat/{workspace_id}/{chat_id}/{message_id}/related_question` |

Two consequences for the tool design:

- **Use the non-streaming answer endpoint.** MCP tool calls are request/response; the
  SSE variant buys nothing and complicates the client.
- **RAG context is `rag_ids` in chat settings**, not a per-question argument. So
  `appflowy_ai(command="ask", page_ids=[...])` sets settings first, then asks.

### Markdown import — there is NO markdown import API

`src/api/data_import.rs` exposes `POST /api/import` (multipart ZIP, headers
`X-Content-Length` / `X-Content-MD5` / `X-Host`), `GET /api/import`, and
`POST /api/import/create` (presigned S3). The import type is **`"notion"`**. No
Markdown importer exists server-side.

So "Markdown import" is **client-side composition** over the existing
markdown→block-tree converter, not a new API binding.

**This server is remote (streamable HTTP), not local stdio.** A filesystem path
argument would address the *server's* disk, not the caller's — useless and a
directory-traversal risk. Therefore:

- `appflowy_import` takes the tree **in the payload**: `entries: [{path, markdown}]`,
  creating parents before children and mapping `path` to the page hierarchy.
- `appflowy_export` returns a page/space subtree as `[{path, markdown}]`, with `depth`
  and `limit` caps so a large space cannot blow up the context window.

(Reference implementations that take filesystem paths — e.g. `weironz/appflowy_mcp` —
are stdio servers run locally via `uvx`, where that choice is correct. It is not
correct here.)

### Still absent from AppFlowy Cloud (re-checked, unchanged)

- No REST endpoint to edit or delete an arbitrary existing block — append-only.
- No REST export endpoint; the desktop app exports client-side.

The pycrdt/collab layer therefore remains **required**, not a design preference. Keep
`pycrdt==0.13.0` pinned (0.14.1 regressed database-collab decoding).

---

## Implementation order

1. Async core: `_login`, `_refresh`, `_api_call` → `httpx.AsyncClient`; all verbs `async def`.
2. Pydantic input models per verb, `Field(...)` with constraints and examples.
3. The 12 verbs, replacing all 35 tools (clean break — no aliases; the point is a small surface).
4. `appflowy_ai`, `appflowy_import`, `appflowy_export`.
5. Pagination + `response_format` on every list-shaped result; drop the deprecated SSE mount.
6. Evaluations: 10 independent, read-only, verifiable QA pairs in `evaluation.xml`.
7. Docs: README migration table, CHANGELOG 2.0.0, `KNOWLEDGE.md` rewrite, version bump.

## Compatibility

**v2.0.0 is a breaking change** — every tool is renamed. No aliases: keeping 35 shims
alongside 12 verbs would leave 47 tools in the manifest and defeat the purpose. The
README ships the full old→new migration table.

## Non-goals

- Migrating to `mcp` 2.x (which removed `mcp.server.fastmcp`). Tracked separately; v2
  stays on the pinned `mcp>=1.13,<2`.
- Streaming AI answers.
- Person mentions — AppFlowy's editor has no person-mention type.
