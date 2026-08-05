"""AppFlowy MCP Server.

An MCP server over the AppFlowy Cloud REST API. Exposes workspace/folder/database
reads plus row create + upsert so an AI agent can both see and *finish* work
(e.g. move a Board card by upserting its Status cell).

Transports:
  * stdio             — run this file directly (local use).
  * Streamable HTTP   — mounted at /mcp   (current MCP standard).
  * SSE               — mounted at /sse   (legacy; kept for existing clients).

Security model (see README/SECURITY):
  * AppFlowy Cloud has no scoped API keys — auth is full-account GoTrue login.
    Use a DEDICATED bot account invited to only the workspace(s) you expose.
  * ALLOWED_WORKSPACE_IDS pins the server to specific workspace(s); every tool
    refuses any other id (defence-in-depth on top of account isolation).
  * MCP_SECRET_TOKEN gates every HTTP request. Send it as an `Authorization:
    Bearer` header (preferred), OR as a `?token=` URL query param ("link method"
    — for UIs like Claude's connector dialog that can't set a header). With the
    link method the token rides in the URL and can appear in logs, so treat the
    whole link like a password.
"""

import base64
import hmac
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pycrdt import Array, Doc, Map, Text

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

BASE_URL = os.environ.get("APPFLOWY_BASE_URL", "https://beta.appflowy.cloud").rstrip(
    "/"
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Sent to MCP clients as server instructions — teaches the agent AppFlowy's model,
# the tool set, the block schema, and the limits so it builds clean structures.
APPFLOWY_INSTRUCTIONS = """\
AppFlowy MCP — you can READ and BUILD in this AppFlowy workspace.

MODEL: workspace → Spaces → pages. A page is a Document, or a Database shown as a
Grid/Board/Calendar/List/Gallery. A Board's columns are a SingleSelect field;
cards are rows. IMPORTANT: a view_id (from get_workspace_folder) is NOT a
database_id — call list_databases to map a view to its database_id before using
row/field tools.

CREATE (clean JSON — the user should never have to set anything up by hand):
create_page, append_blocks, create_database (grid/board/calendar),
create_database_view, add_database_field, create_database_row,
upsert_database_row, create_space, move_page, duplicate_page, trash_page.

EDIT/DELETE ANY ROW (Tier 2 / collab — works even on UI-created rows):
update_row_cells (change cells on any existing row — e.g. move a Board card by
setting its Status cell to the target option id), delete_row (hard-delete a row).

EDIT SCHEMA: update_database_field (rename a column) and delete_database_field
(drop it) — Tier 2/collab, no REST. add_select_option (returns the new option id —
use THAT as the cell value) and delete_select_option manage SingleSelect/MultiSelect
options. set_group_by(view_id, field_id) sets which field a Board groups its columns
by. rename_page renames a page/database/space; restore_page un-trashes a page.

CONTENT IS MARKDOWN EVERYWHERE. create_page(markdown=...), append_blocks(markdown=...),
and a row/card `document` all take standard Markdown, rendered into real blocks:
#/##/### headings, "- "/"1. " lists (nestable), "- [ ]" checkboxes, "> " quote,
```lang fenced code, "---" divider, images, links, GFM callouts (> [!NOTE]/[!TIP]/
[!WARNING]…), $$ math blocks, and inline **bold**/*italic*/~~strike~~/`code`. Prefer
markdown for any prose or card body. READ any page or card
body back AS Markdown with get_page_markdown(page_id) — the inverse (page view id or row id).
MENTION a page inline with [](mention:<view_id>) — renders as a live link to that page.

For blocks Markdown can't express, pass a page_data JSON block tree instead, e.g.
{"type":"page","children":[
  {"type":"heading","data":{"level":1,"delta":[{"insert":"Title"}]}},
  {"type":"todo_list","data":{"delta":[{"insert":"task"}],"checked":false}},
  {"type":"divider"}]}
Block types: paragraph, heading(data.level 1-6), bulleted_list, numbered_list,
todo_list(data.checked), quote, code(data.language), divider, image(data.url).
Delta attrs: bold, italic, underline, strikethrough, code, color, href.

FIELD TYPES (add_database_field): 0=RichText 1=Number 2=DateTime 3=SingleSelect
4=MultiSelect 5=Checkbox 6=URL 7=Checklist 8=LastEditedTime 9=CreatedTime.

EDIT DOCUMENT BLOCKS (Tier 2 / collab): add_block (any type incl. ADVANCED —
callout, toggle_list, quote, code, heading; pass block-specific `data`),
edit_block_text (both render inline **Markdown**: bold/italic/code/strike/links),
replace_text (find-and-replace by content — NO block id needed), delete_block. page_id may be a document view id OR a database ROW
id — a row id auto-resolves to the card's BODY document, so this is how you add a
checkbox/sub-task to a Board card. Put per-card checklists in the card BODY, never
in a shared column (a column value shows on every card).

BEST PRACTICE: To update a row in place instead of creating a duplicate, upsert with a
stable pre_hash and reuse that pre_hash on later calls. Keep cells for title/status/
metadata and put long content or checklists in a row's BODY document, never in a shared
column (a column value shows on every row).

AVOID: (1) Trusting an immediate re-read — get_database_row_details reads /row/detail,
a materialized view that LAGS the live data by up to minutes; a write can be correct even
when the re-read still shows the old value, so don't conclude it failed. (2) Guessing a
database_id from a folder view_id (use list_databases). (3) Full-overwriting a document —
the edit tools send merging updates; never PUT a whole collab.

LIMITS: in-place edits render inline Markdown (bold/italic/code/strike/links) but not
text color or underline. NOT SUPPORTED YET (roadmap — see KNOWLEDGE.md §9 Coverage):
columns, toggle headings, table of contents, @mentions, link-to-page, web-bookmark,
Drive/iframe embed, file/video/audio upload, List/Gallery/Chart/Feed views, and inline
or linked database views. AI blocks (AI note/summarize/ask) run AppFlowy's own AI and
aren't insertable content. Full guide + coverage matrix: MCP resource appflowy://guide
(also KNOWLEDGE.md in the repo).
"""

# Server logo (three kanban columns on AppFlowy purple). Declared in the MCP
# initialize response and served at /icon.svg. NOTE: Claude.ai currently shows a
# generic globe for all custom connectors regardless — this is for spec
# compliance, other clients, and future support.
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
    '<rect width="128" height="128" rx="28" fill="#A34AFD"/>'
    '<rect x="30" y="34" width="18" height="60" rx="6" fill="#fff"/>'
    '<rect x="55" y="34" width="18" height="42" rx="6" fill="#fff" fill-opacity="0.9"/>'
    '<rect x="80" y="34" width="18" height="30" rx="6" fill="#fff" fill-opacity="0.8"/>'
    "</svg>"
)
ICON_DATA_URI = (
    "data:image/svg+xml;base64," + base64.b64encode(ICON_SVG.encode()).decode()
)


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


# DNS-rebinding protection guards against browser-driven attacks on the HTTP
# transports. Enabled by default; set MCP_ALLOWED_HOSTS to your public host
# (e.g. mcp.example.com) when behind a reverse proxy, or MCP_DNS_REBINDING_
# PROTECTION=false as an escape hatch if a proxy setup misreports Host.
_dns_protect = os.environ.get("MCP_DNS_REBINDING_PROTECTION", "true").lower() != "false"
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=_dns_protect,
    allowed_hosts=_csv_env("MCP_ALLOWED_HOSTS")
    or ["localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"],
    allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS")
    or ["http://localhost", "http://127.0.0.1"],
)

# streamable_http_path="/" so mounting the app at /mcp yields a clean /mcp/
# endpoint (not the doubled /mcp/mcp you'd get with the default path).
mcp = FastMCP(
    "appflowy-mcp",
    instructions=APPFLOWY_INSTRUCTIONS,
    icons=[{"src": ICON_DATA_URI, "mimeType": "image/svg+xml", "sizes": ["any"]}],
    website_url="https://github.com/Rahulk644/appflowy-mcp-server",
    transport_security=_transport_security,
    streamable_http_path="/",
)


# Full operator guide (KNOWLEDGE.md) exposed as an MCP resource, so an agent can pull
# the deep reference — tools-by-task, recipes, pitfalls, coverage matrix, data model —
# on demand instead of it bloating every tool description (keeps the tool surface lean).
def _agent_guide_md() -> str:
    return (Path(__file__).parent / "KNOWLEDGE.md").read_text(encoding="utf-8")


@mcp.resource(
    "appflowy://guide",
    name="AppFlowy Agent Guide",
    description="Operator guide: tools-by-task, recipes, pitfalls, coverage matrix, data model.",
    mime_type="text/markdown",
)
def agent_guide() -> str:
    return _agent_guide_md()


# Tool behavior hints for MCP clients (advisory — they help an agent choose safe
# operations; they are NOT a security boundary). readOnly = performs no writes;
# destructive = makes an irreversible change; idempotent = repeating with the same
# args reaches the same state; openWorld = calls the external AppFlowy service (always
# true here). Presets are applied per tool via @mcp.tool(annotations=...).
_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_CREATE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}

# ---- Optional OAuth (Google-federated) — active only if GOOGLE_CLIENT_ID set --
OAUTH_ISSUER = os.environ.get("OAUTH_ISSUER", "").rstrip("/")
_oauth_provider = None
if os.environ.get("GOOGLE_CLIENT_ID") and OAUTH_ISSUER:
    from google_oauth import GoogleOAuthProvider

    _oauth_provider = GoogleOAuthProvider(
        issuer=OAUTH_ISSUER,
        google_client_id=os.environ["GOOGLE_CLIENT_ID"],
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        allowed_emails=_csv_env("ALLOWED_EMAILS"),
        store_path=os.environ.get("OAUTH_STORE_PATH") or None,
    )

# Token management
_access_token = None
_refresh_token = None
_token_expires_at = 0


def _allowed_workspaces() -> set[str] | None:
    """Workspace ids this server may touch, or None if unrestricted."""
    ids = _csv_env("ALLOWED_WORKSPACE_IDS")
    return set(ids) if ids else None


def _require_workspace(workspace_id: str) -> None:
    allowed = _allowed_workspaces()
    if allowed is not None and workspace_id not in allowed:
        raise ValueError(
            f"Workspace '{workspace_id}' is not permitted by this server's "
            "ALLOWED_WORKSPACE_IDS policy."
        )


def _login() -> None:
    global _access_token, _refresh_token, _token_expires_at
    email = os.environ.get("APPFLOWY_EMAIL")
    password = os.environ.get("APPFLOWY_PASSWORD")

    if not email or not password:
        raise ValueError("APPFLOWY_EMAIL and APPFLOWY_PASSWORD must be set.")

    url = f"{BASE_URL}/gotrue/token?grant_type=password"
    data = {"email": email, "password": password}

    with httpx.Client() as client:
        res = client.post(url, json=data, headers={"User-Agent": USER_AGENT})
        res.raise_for_status()

        body = res.json()
        _access_token = body.get("access_token")
        _refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in", 3600)
        _token_expires_at = time.time() + expires_in - 60  # 60s buffer


def _refresh() -> None:
    global _access_token, _refresh_token, _token_expires_at
    if not _refresh_token:
        _login()
        return

    url = f"{BASE_URL}/gotrue/token?grant_type=refresh_token"
    data = {"refresh_token": _refresh_token}

    with httpx.Client() as client:
        res = client.post(url, json=data, headers={"User-Agent": USER_AGENT})
        if res.status_code != 200:
            _login()  # refresh token expired -> re-login
            return

        body = res.json()
        _access_token = body.get("access_token")
        if body.get("refresh_token"):
            _refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in", 3600)
        _token_expires_at = time.time() + expires_in - 60


def get_auth_headers() -> dict:
    if not _access_token or time.time() >= _token_expires_at:
        try:
            _refresh() if _refresh_token else _login()
        except Exception:  # noqa: BLE001 - any refresh failure (expired/revoked/
            # malformed token, transport error) is recoverable by a full re-login.
            _login()

    return {
        "Authorization": f"Bearer {_access_token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


# Status-code → what the agent should do about it. Surfaced in tool errors so a
# failed call is self-explanatory instead of a bare stack trace.
_ERROR_HINTS = {
    400: "check the ids and the JSON payload shape",
    401: "the server's AppFlowy login is invalid or expired",
    403: "the account lacks access to this workspace or resource",
    404: "verify the id exists — a folder view_id is NOT a database_id (call list_databases)",
    409: "the resource already exists or was modified concurrently",
    413: "the payload is too large — split it into smaller writes",
    429: "rate limited — wait briefly and retry",
}


def _api_call(method: str, path: str, **kwargs) -> httpx.Response:
    """Authenticated AppFlowy API call with actionable errors. `path` is joined to
    BASE_URL. Raises RuntimeError with a specific, agent-readable message on failure so a
    tool error tells the agent how to fix its call rather than dumping a raw traceback."""
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.request(
                method, f"{BASE_URL}{path}", headers=get_auth_headers(), **kwargs
            )
            res.raise_for_status()
            return res
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        hint = _ERROR_HINTS.get(code, "unexpected AppFlowy API error")
        body = " ".join(e.response.text[:200].split())
        raise RuntimeError(f"AppFlowy API {code}: {hint}. Server said: {body}") from e
    except httpx.RequestError as e:
        raise RuntimeError(
            f"AppFlowy API request did not complete ({type(e).__name__}) — retry shortly"
        ) from e


def _post(path: str, body: dict):
    """POST JSON to the AppFlowy API; return the response's `data` field."""
    return _api_call("POST", path, json=body).json().get("data", "")


# ---- Collab / CRDT layer (Tier 2): edit/delete any block or row --------------
# The REST API is create/append-only. For surgical edits we fetch the object's
# yrs collab, mutate it with pycrdt, and POST the *diff* to the merging web-update
# endpoint (never the full-overwrite PUT, which clobbers concurrent edits).
# collab_type: 0=Document, 1=Database, 5=DatabaseRow.


def _collab_doc_state(payload) -> bytes:
    """Extract the yrs `doc_state` bytes from AppFlowy's collab GET response.

    AppFlowy Cloud has changed this response shape across releases, so be tolerant:
    the body may or may not be wrapped in an `AppResponse` `{code,message,data}`
    envelope, the encoded collab may sit under `encode_collab`/`encoded_collab` or at
    the top level, and `doc_state` is a byte array (list[int]) in older builds or a
    base64 string in newer ones. If none of the known shapes match, fail loudly with
    the actual keys — an opaque `KeyError: 'data'` is what broke us, so make the next
    shape change a five-second fix instead.
    """
    body = payload.get("data", payload) if isinstance(payload, dict) else payload
    node = body
    if isinstance(node, dict):
        for key in ("encode_collab", "encoded_collab", "encode_collab_v1", "collab"):
            if isinstance(node.get(key), dict):
                node = node[key]
                break
    ds = node.get("doc_state") if isinstance(node, dict) else None
    if ds is None and isinstance(body, dict):
        ds = body.get("doc_state")
    if ds is None:
        shape = (
            list(payload.keys())
            if isinstance(payload, dict)
            else type(payload).__name__
        )
        raise RuntimeError(
            f"AppFlowy collab response has no doc_state (response keys={shape}); "
            "the collab endpoint shape changed — update _collab_doc_state()."
        )
    return base64.b64decode(ds) if isinstance(ds, str) else bytes(ds)


def _collab_doc(workspace_id: str, object_id: str, collab_type: int) -> Doc:
    """Fetch a collab object and load it into a pycrdt Doc."""
    res = _api_call(
        "GET",
        f"/api/workspace/v1/{workspace_id}/collab/{object_id}",
        params={"collab_type": collab_type},
    )
    doc = Doc()
    doc.apply_update(_collab_doc_state(res.json()))
    return doc


def _collab_web_update(
    workspace_id: str, object_id: str, doc: Doc, state_vector: bytes, collab_type: int
) -> None:
    """POST the yrs update diff (changes since state_vector) — the server merges it."""
    update = doc.get_update(state_vector)
    _api_call(
        "POST",
        f"/api/workspace/v1/{workspace_id}/collab/{object_id}/web-update",
        json={"doc_state": list(update), "collab_type": collab_type},
    )


def _nid(n: int = 10) -> str:
    """AppFlowy-style short id for new blocks/text-map keys."""
    return "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(n)
    )


def _row_document_id(row_id: str) -> str:
    """A database row's body document (the card page below the row) is a separate
    collab; AppFlowy derives its object id as uuid5(row_uuid, "document_id")."""
    return str(uuid.uuid5(uuid.UUID(row_id), "document_id"))


def _is_database_row(workspace_id: str, view_id: str) -> bool:
    """True if `view_id` addresses a database ROW rather than a page-view document.

    The page-view endpoint carries `row_data` for a row and `encoded_collab` for a
    document — the cheapest reliable discriminator (one small REST GET, rather than
    downloading the whole collab just to see whether it decodes). Both the flat and
    nested response shapes are accepted, since AppFlowy has moved this around before.

    Never raises: this only ever *adds* a fallback path, so an unreachable or
    unrecognised response answers False and leaves the caller's behaviour unchanged.
    """
    try:
        meta = (
            _api_call("GET", f"/api/workspace/{workspace_id}/page-view/{view_id}")
            .json()
            .get("data", {})
        )
    except Exception:  # noqa: BLE001 - a probe, never a failure mode of its own
        return False
    if not isinstance(meta, dict):
        return False
    node = meta.get("data") if isinstance(meta.get("data"), dict) else meta
    return isinstance(node, dict) and "row_data" in node


# AppFlowy CollabType enum: Document=0, Database=1, DatabaseRow=4, UserAwareness=5.
# A database ROW collab is type 4. Older AppFlowy ignored collab_type and keyed on the
# object_id, so the server's earlier `5` happened to work; newer AppFlowy enforces the
# enum and rejects the wrong type with `Record ... not active`.
_ROW_COLLAB = 4


def _open_document(workspace_id: str, page_id: str):
    """Load a document collab for block editing. `page_id` may be the document's own
    view id OR a database row id — a row id is transparently resolved to the row's
    body document (so agents can edit a Board card's checklist by its row id).
    Returns (doc, object_id, document_map); pass object_id back to _collab_web_update."""
    root = None
    try:
        doc = _collab_doc(workspace_id, page_id, 0)
        root = doc.get("data", type=Map)
    except RuntimeError:
        # `page_id` is a database row id, not a Document collab. AppFlowy rejects a row
        # id fetched as collab_type 0 ("Record ... not active") instead of returning the
        # row collab, so fall through and resolve to the row's hidden body document.
        pass
    if root is None or "document" not in root:
        page_id = _row_document_id(page_id)
        doc = _collab_doc(workspace_id, page_id, 0)
        root = doc.get("data", type=Map)
    return doc, page_id, root["document"]


def _open_database(workspace_id: str, database_id: str):
    """Load a database collab (type 1); return (doc, root) where root is the DATABASE
    map holding 'fields' (field_id -> field) and 'views' (view_id -> view)."""
    doc = _collab_doc(workspace_id, database_id, 1)
    return doc, doc.get("data", type=Map)["database"]


def _to_yjs(v):
    """Recursively wrap plain JSON into pycrdt shared types for collab insertion."""
    if isinstance(v, dict):
        return Map({k: _to_yjs(x) for k, x in v.items()})
    if isinstance(v, list):
        return Array([_to_yjs(x) for x in v])
    return v


def _field_types(workspace_id: str, database_id: str) -> dict:
    """Map field_id -> numeric field type. Prefer the DB collab (authoritative, no
    lag); a new cell must be tagged with the right type or the write appears to silently
    drop. Fall back to the REST fields view (which lags for just-created fields) only if
    the collab can't be read."""
    try:
        _, root = _open_database(workspace_id, database_id)
        fields = root["fields"]
        return {fid: int(fields[fid]["ty"]) for fid in fields if "ty" in fields[fid]}
    except Exception:  # noqa: BLE001 - the collab read is the authoritative source but
        # can fail for any reason (schema drift, decode error); fall back to REST, which
        # lags for just-created fields but is better than failing the whole call.
        return {
            f["id"]: int(f.get("field_type_id", 0))
            for f in get_database_fields(workspace_id, database_id)
        }


# Select fields store their whole SelectTypeOption as a JSON STRING under
# type_option["<field_type>"]["content"] = {"options":[{id,name,color}], "disable_color"}.
# color must be one of these exact names — an invalid value makes AppFlowy's
# deserialize fail and silently drop every option, so we validate strictly.
_SELECT_TYPES = {3, 4}  # 3=SingleSelect, 4=MultiSelect
SELECT_COLORS = [
    "Purple",
    "Pink",
    "LightPink",
    "Orange",
    "Yellow",
    "Lime",
    "Green",
    "Aqua",
    "Blue",
    "Cream",
    "Mint",
    "Sky",
    "Lilac",
    "Pearl",
    "Sunset",
    "Coral",
    "Sapphire",
    "Moss",
    "Sand",
    "Charcoal",
]


def _read_select(field):
    """Parse a select field's options. Returns (type_key, data) where data is
    {"options":[{id,name,color}], "disable_color":bool}. Raises if not a select."""
    ty = int(field["ty"]) if "ty" in field else -1
    if ty not in _SELECT_TYPES:
        raise ValueError("field is not a SingleSelect/MultiSelect column")
    tk = str(ty)
    content = ""
    to = field.get("type_option", None)
    if to is not None and tk in to and "content" in to[tk]:
        content = to[tk]["content"]
    data = json.loads(content) if content else {}
    data.setdefault("options", [])
    data.setdefault("disable_color", False)
    return tk, data


def _write_select(field, tk, data):
    """Serialize the options dict back into type_option[tk]["content"] (a JSON string)."""
    if "type_option" not in field:
        field["type_option"] = Map()
    to = field["type_option"]
    if tk not in to:
        to[tk] = Map()
    to[tk]["content"] = json.dumps(data, separators=(",", ":"))


@mcp.tool(annotations=_READ)
def get_workspaces() -> list:
    """Retrieves your AppFlowy workspaces (filtered to ALLOWED_WORKSPACE_IDS if set)."""
    data = _api_call("GET", "/api/workspace").json().get("data", [])
    allowed = _allowed_workspaces()
    if allowed is not None:
        data = [w for w in data if (w.get("workspace_id") or w.get("id")) in allowed]
    return data


@mcp.tool(annotations=_READ)
def get_workspace_folder(workspace_id: str, depth: int = 1) -> dict:
    """
    Fetches the folder structure (pages and databases) of a workspace.
    Useful for finding the database_id to query.
    """
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/folder"
    return _api_call("GET", path, params={"depth": depth}).json().get("data", {})


@mcp.tool(annotations=_READ)
def get_database_fields(workspace_id: str, database_id: str) -> list:
    """Retrieves the fields/columns available in a database."""
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/database/{database_id}/fields"
    return _api_call("GET", path).json().get("data", [])


@mcp.tool(annotations=_READ)
def get_database_row_ids(workspace_id: str, database_id: str) -> list:
    """Retrieves the row IDs in a database."""
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/database/{database_id}/row"
    return _api_call("GET", path).json().get("data", [])


@mcp.tool(annotations=_READ)
def get_database_row_details(
    workspace_id: str, database_id: str, row_ids: str, with_doc: bool = False
) -> list:
    """
    Retrieves detailed content for specific database rows.
    row_ids: comma-separated row UUIDs (e.g. 'uuid1,uuid2').
    """
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/database/{database_id}/row/detail"
    params = {"ids": row_ids}
    if with_doc:
        params["with_doc"] = "true"
    return _api_call("GET", path, params=params).json().get("data", [])


@mcp.tool(annotations=_CREATE)
def create_database_row(workspace_id: str, database_id: str, row_data: str) -> str:
    """
    Creates a new row in a database.
    row_data: JSON string with keys `cells` (field_id/field_name -> value) and
    optional `document` (Markdown). Returns the new row id.
    """
    _require_workspace(workspace_id)
    try:
        data = json.loads(row_data)
    except json.JSONDecodeError as exc:
        raise ValueError("row_data must be a valid JSON string") from exc
    path = f"/api/workspace/{workspace_id}/database/{database_id}/row"
    return _api_call("POST", path, json=data).json().get("data", "")


@mcp.tool(annotations=_WRITE)
def upsert_database_row(workspace_id: str, database_id: str, row_data: str) -> str:
    """
    Creates or updates (upserts) a row — the idempotent way to write. Use it to
    update a row in place (e.g. move a Board card by setting its Status cell) without
    creating duplicates on re-runs.

    row_data: JSON string with keys:
      * pre_hash (str): identifies the row. Reuse an existing row's pre_hash to
        UPDATE it (e.g. to set its Status cell and move it between columns);
        a new pre_hash creates a new row.
      * cells (obj): field_id or field_name -> value.
      * document (str, optional): Markdown body.
    Returns the created/updated row id.
    """
    _require_workspace(workspace_id)
    try:
        data = json.loads(row_data)
    except json.JSONDecodeError as exc:
        raise ValueError("row_data must be a valid JSON string") from exc
    path = f"/api/workspace/{workspace_id}/database/{database_id}/row"
    return _api_call("PUT", path, json=data).json().get("data", "")


@mcp.tool(annotations=_READ)
def list_updated_rows(workspace_id: str, database_id: str, after: str = "") -> list:
    """
    Lists rows updated in a database (change feed) — useful for syncing "what
    changed" into a pipeline.
    after: optional cursor/timestamp forwarded to AppFlowy's /row/updated.
    """
    # ponytail: `after` semantics come straight from AppFlowy's /row/updated;
    # confirm the exact cursor format against a live workspace before relying on it.
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/database/{database_id}/row/updated"
    params = {"after": after} if after else {}
    return _api_call("GET", path, params=params).json().get("data", [])


# ---- Structure & document tools (create/manage pages, databases, blocks) ----

_DB_LAYOUTS = {"grid": 1, "board": 2, "calendar": 3}


@mcp.tool(annotations=_READ)
def list_databases(workspace_id: str) -> list:
    """Lists databases in the workspace with their id and views. Use this to map a
    Board/Grid view to its database_id (a folder view_id is NOT the database_id)."""
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/database"
    return _api_call("GET", path).json().get("data", [])


# ---- Markdown → AppFlowy block tree ---------------------------------------
# AppFlowy Cloud converts Markdown → blocks server-side for ROW bodies, but the
# page-view create endpoint only accepts a structured block tree. So we do what
# Notion's MCP does: parse Markdown here (markdown-it, the reference CommonMark
# parser) and emit the block tree create_page / append_blocks feed to AppFlowy —
# one Markdown content interface everywhere. Covers the basic-block palette;
# unknown constructs fall back to a paragraph so text is never silently dropped.
# ponytail: MVP palette — callouts/toggles/columns and tables-as-blocks are
# roadmap (a GFM table degrades to its plaintext for now).

# dollarmath is configured with allow_digits/allow_space OFF, which is a data-loss
# fix, not a preference. With the defaults, any prose containing two dollar AMOUNTS
# has everything between them swallowed into a math node:
#   "ARR hit $560k this quarter, up from $380k last year."
#     -> "ARR hit 380k last year."          <- figures and words silently gone
# Money in notes is far more common than inline TeX, and disallowing a digit or a
# space adjacent to the delimiter kills the money case while keeping real math:
# "$e^{i\pi}+1=0$" still parses, and $$...$$ block math is untouched.
_md_parser = (
    MarkdownIt("commonmark")
    .enable(["table", "strikethrough"])
    .use(dollarmath_plugin, allow_digits=False, allow_space=False)
)

# GFM alert types (> [!NOTE]) <-> AppFlowy callout icons, for round-tripping callouts.
_ALERT_ICONS = {
    "note": "📝",
    "tip": "💡",
    "important": "❗",
    "warning": "⚠️",
    "caution": "🔥",
}
_ICON_ALERTS = {v: k for k, v in _ALERT_ICONS.items()}

_INLINE_ATTR = {"strong": "bold", "em": "italic", "s": "strikethrough"}
_TASK_PREFIXES = {"[ ] ": False, "[x] ": True, "[X] ": True}


def _delta_op(text: str, attrs: dict) -> dict:
    op = {"insert": text}
    if attrs:
        op["attributes"] = dict(attrs)
    return op


def _inline_delta(node) -> list:
    """A markdown-it 'inline' node -> AppFlowy delta (list of insert ops)."""
    ops: list = []

    def walk(children, attrs):
        for c in children:
            t = c.type
            if t == "text":
                if c.content:
                    ops.append(_delta_op(c.content, attrs))
            elif t in ("softbreak", "hardbreak"):
                ops.append(_delta_op("\n", attrs))
            elif t == "code_inline":
                ops.append(_delta_op(c.content, {**attrs, "code": True}))
            elif t in _INLINE_ATTR:
                walk(c.children, {**attrs, _INLINE_ATTR[t]: True})
            elif t == "link":
                href = c.attrs.get("href", "")
                if href.startswith("mention:"):  # [](mention:<page_id>) -> page mention
                    ops.append(
                        {
                            "insert": "$",
                            "attributes": {
                                "mention": {"type": "page", "page_id": href[8:]}
                            },
                        }
                    )
                else:
                    walk(c.children, {**attrs, "href": href})
            elif t == "image":
                alt = c.content or c.attrs.get("alt", "")
                if alt:
                    ops.append(_delta_op(alt, attrs))
            elif t == "math_inline":
                ops.append(_delta_op(f"${c.content}$", attrs))
            elif c.children:
                walk(c.children, attrs)
            elif c.content:
                ops.append(_delta_op(c.content, attrs))

    walk(node.children, {})
    return ops


def _first_inline(node):
    for c in node.children:
        if c.type == "inline":
            return c
    return None


def _delta_of(node) -> list:
    inl = _first_inline(node)
    return _inline_delta(inl) if inl else []


def _sole_image_url(para_node):
    """If a paragraph is a lone image, return its src (AppFlowy image is a block,
    not inline); else None."""
    inl = _first_inline(para_node)
    if not inl:
        return None
    imgs = [c for c in inl.children if c.type == "image"]
    other = [
        c
        for c in inl.children
        if c.type not in ("image", "softbreak", "hardbreak")
        and (c.content or c.children)
    ]
    return imgs[0].attrs.get("src", "") if len(imgs) == 1 and not other else None


def _task_state(delta):
    """If the delta starts with a GFM task marker, return (checked, stripped_delta);
    else None."""
    if not delta or "attributes" in delta[0]:
        return None
    lead_ops, lead = 0, ""
    for o in delta:
        if "attributes" in o:
            break
        lead += o["insert"]
        lead_ops += 1
    for pref, checked in _TASK_PREFIXES.items():
        if lead.startswith(pref):
            remainder = lead[len(pref) :]
            rest = delta[lead_ops:]
            new = ([{"insert": remainder}] if remainder else []) + [
                dict(o) for o in rest
            ]
            return checked, (new or [{"insert": ""}])
    return None


def _node_plaintext(node) -> str:
    if node.type in ("fence", "code_block"):
        return node.content.rstrip("\n")
    if not node.children:
        return node.content or ""
    return "".join(_node_plaintext(c) for c in node.children)


def _drop_first_line(delta) -> list:
    """Remove the first text line (through the first newline) from a delta — strips the
    "[!NOTE]" marker line off a GFM alert before the rest becomes the callout body."""
    out, dropping = [], True
    for op in delta:
        if not dropping:
            out.append(op)
            continue
        nl = op["insert"].find("\n")
        if nl == -1:
            continue  # whole op is on the first line -> drop it
        dropping = False
        rest = op["insert"][nl + 1 :]
        if rest:
            out.append({**op, "insert": rest})
    return out


def _list_item_block(li, list_type):
    delta, children = [], []
    for c in li.children:
        if c.type == "paragraph" and not delta and not children:
            delta = _delta_of(c)
        else:
            children.extend(_block_from_node(c))
    task = _task_state(delta)
    if task is not None:
        block = {"type": "todo_list", "data": {"checked": task[0], "delta": task[1]}}
    else:
        block = {"type": list_type, "data": {"delta": delta or [{"insert": ""}]}}
    if children:
        block["children"] = children
    return block


def _block_from_node(node) -> list:
    """Map one markdown-it block node -> zero or more AppFlowy blocks."""
    t = node.type
    if t == "heading":
        lvl = min(max(int(node.tag[1:]), 1), 6)
        return [{"type": "heading", "data": {"level": lvl, "delta": _delta_of(node)}}]
    if t == "paragraph":
        url = _sole_image_url(node)
        if url:
            return [{"type": "image", "data": {"url": url}}]
        return [{"type": "paragraph", "data": {"delta": _delta_of(node)}}]
    if t == "hr":
        return [{"type": "divider", "data": {}}]
    if t in ("fence", "code_block"):
        lang = (node.info or "").strip().split(" ")[0] if t == "fence" else ""
        code = node.content.rstrip("\n")
        return [
            {"type": "code", "data": {"language": lang, "delta": [{"insert": code}]}}
        ]
    if t == "math_block":
        return [{"type": "math_equation", "data": {"formula": node.content.strip()}}]
    if t == "blockquote":
        parts = [_delta_of(c) for c in node.children if c.type == "paragraph"]
        delta = []
        for i, p in enumerate(parts):
            if i:
                delta.append({"insert": "\n"})
            delta.extend(p)
        # A GFM alert ("> [!NOTE]") becomes an AppFlowy callout; else a plain quote.
        lead = delta[0]["insert"] if delta and "attributes" not in delta[0] else ""
        marker = lead.split("\n", 1)[0].strip().lower()
        for name, icon in _ALERT_ICONS.items():
            if marker == f"[!{name}]":
                body = _drop_first_line(delta)
                return [
                    {
                        "type": "callout",
                        "data": {"icon": icon, "delta": body or [{"insert": ""}]},
                    }
                ]
        return [{"type": "quote", "data": {"delta": delta or [{"insert": ""}]}}]
    if t == "bullet_list":
        return [_list_item_block(li, "bulleted_list") for li in node.children]
    if t == "ordered_list":
        return [_list_item_block(li, "numbered_list") for li in node.children]
    text = _node_plaintext(node)
    return (
        [{"type": "paragraph", "data": {"delta": [{"insert": text}]}}] if text else []
    )


def _md_to_blocks(markdown: str) -> list:
    """Parse Markdown into an AppFlowy page block-tree (list of block dicts)."""
    tree = SyntaxTreeNode(_md_parser.parse(markdown or ""))
    blocks: list = []
    for node in tree.children:
        blocks.extend(_block_from_node(node))
    return blocks


# ---- AppFlowy block tree → Markdown (the inverse of _md_to_blocks) ---------
# Reads a document collab's blocks and renders Markdown, so an agent can pull a
# page or card body back out in the format it writes (symmetric with
# create_page(markdown=...)). Inline styles come from each yjs Text's diff() —
# segments of (text, {bold|italic|strikethrough|code|href}); block-type fields
# (level, checked, language, url) come from the block's `data` JSON.

# Markdown list "family": bullet-style items (- / - [ ]) pack tight together, but a
# switch to/from an ordered (1.) list needs a blank line so it re-parses cleanly.
_LIST_FAMILY = {"bulleted_list": "ul", "todo_list": "ul", "numbered_list": "ol"}


def _inline_md(delta) -> str:
    out = []
    for text, attrs in delta or []:
        a = attrs or {}
        mention = a.get("mention")
        if mention and mention.get("page_id"):  # page mention -> [](mention:<id>)
            out.append(f"[](mention:{mention['page_id']})")
            continue
        if a.get("code"):
            seg = f"`{text}`"
        else:
            seg = text
            if a.get("bold"):
                seg = f"**{seg}**"
            if a.get("italic"):
                seg = f"*{seg}*"
            if a.get("strikethrough"):
                seg = f"~~{seg}~~"
        if a.get("href"):
            seg = f"[{seg}]({a['href']})"
        out.append(seg)
    return "".join(out)


def _render_block(bid, blocks, cmap, tmap, depth):
    """Return (lines, is_list_item) for a block and its (indented) children."""
    b = blocks[bid]
    ty = b.get("ty", "")
    data = json.loads(b["data"]) if (b.get("data")) else {}
    ext = b.get("external_id", None)
    delta = tmap[ext].diff() if (ext is not None and ext in tmap) else []
    pad = "  " * depth

    if ty == "code":
        raw = "".join(t for t, _ in delta)
        lines = [f"```{data.get('language', '')}", *raw.split("\n"), "```"]
    elif ty == "divider":
        lines = ["---"]
    elif ty == "image":
        lines = [f"![]({data.get('url', '')})"]
    elif ty == "math_equation":
        lines = ["$$", data.get("formula", ""), "$$"]
    else:
        text = _inline_md(delta)
        if ty == "heading":
            lines = ["#" * int(data.get("level", 1)) + " " + text]
        elif ty == "bulleted_list":
            lines = [f"{pad}- {text}"]
        elif ty == "numbered_list":
            lines = [f"{pad}1. {text}"]
        elif ty == "todo_list":
            box = "x" if data.get("checked") else " "
            lines = [f"{pad}- [{box}] {text}"]
        elif ty == "quote":
            lines = [f"> {text}"]
        elif ty == "callout":
            alert = _ICON_ALERTS.get(data.get("icon", ""))
            head = f"> [!{alert.upper()}]" if alert else "> [!NOTE]"
            body = text.split("\n") if text else []
            if not alert and data.get("icon") and body:
                body[0] = f"{data['icon']} {body[0]}"
            lines = [head] + [f"> {ln}" for ln in body]
        elif ty == "toggle_list":  # a level makes it a toggle-heading; no md toggle
            lvl = data.get("level")
            lines = ["#" * int(lvl) + " " + text] if lvl else [f"{pad}- {text}"]
        else:  # paragraph, and any unknown block -> plain text (never dropped)
            lines = [text]

    ck = b.get("children", None)
    if ck is not None and ck in cmap:
        for c in list(cmap[ck]):
            child_lines, _ = _render_block(c, blocks, cmap, tmap, depth + 1)
            lines.extend(child_lines)
    return lines, _LIST_FAMILY.get(ty)


def _doc_to_markdown(document) -> str:
    """Render a document map ({blocks, meta, page_id}) to Markdown."""
    blocks, meta = document["blocks"], document["meta"]
    cmap, tmap = meta["children_map"], meta["text_map"]
    root = blocks[document["page_id"]]
    ck = root.get("children", None)
    if ck is None or ck not in cmap:
        return ""
    parts, prev_fam = [], None
    for i, c in enumerate(list(cmap[ck])):
        lines, fam = _render_block(c, blocks, cmap, tmap, 0)
        block_md = "\n".join(lines)
        if i == 0:
            parts.append(block_md)
        else:
            parts.append(("\n" if fam and fam == prev_fam else "\n\n") + block_md)
        prev_fam = fam
    return "".join(parts).strip() + "\n"


# Code / equation blocks hold literal text; everything else renders inline Markdown.
_PLAIN_TEXT_TYS = {"code", "math_equation"}


def _md_inline_to_delta(text: str) -> list:
    """Parse one line of inline Markdown (**bold**, *italic*, `code`, ~~strike~~,
    [links](url)) into an AppFlowy delta; literal fallback for non-inline input."""
    if not text:
        return []
    blocks = _md_to_blocks(text)
    if len(blocks) == 1 and blocks[0]["type"] == "paragraph":
        return blocks[0]["data"]["delta"]
    return [{"insert": text}]


def _set_text(text_obj, delta) -> None:
    """Overwrite a yjs Text from a delta: plain content, then inline formatting ranges
    (bold/italic/strikethrough/code/href) via Text.format(start, stop). Offsets are in
    UTF-8 BYTES — pycrdt/yrs indexes Text by byte, so a multi-byte char (an emoji is 4
    bytes) must advance the position by its byte length or the format range drifts past
    it (e.g. a leading emoji would mis-place every link on the line)."""
    if len(text_obj) > 0:
        del text_obj[0 : len(text_obj)]
    plain = "".join(op["insert"] for op in delta)
    if plain:
        text_obj.insert(0, plain)
    pos = 0
    for op in delta:
        n = len(op["insert"].encode("utf-8"))
        attrs = op.get("attributes")
        if attrs and n:
            text_obj.format(pos, pos + n, attrs)
        pos += n


@mcp.tool(annotations=_CREATE)
def create_page(
    workspace_id: str,
    parent_view_id: str,
    name: str,
    markdown: str = "",
    page_data: str = "",
) -> str:
    """Creates a Document page under parent_view_id; returns the new view_id.

    Preferred — `markdown`: standard Markdown is rendered into real AppFlowy
    blocks (headings; bulleted / numbered / `- [ ]` task lists incl. nesting;
    quotes; fenced code with language; dividers; images; and inline **bold**,
    *italic*, ~~strike~~, `code`, and [links](url)).
    e.g. markdown="# Plan\\n\\n- [ ] draft\\n- [x] outline".

    Advanced — `page_data`: a raw block-tree JSON
    ({"type":"page","children":[...]}) for blocks Markdown can't express. Pass
    either markdown or page_data (markdown wins if both are given)."""
    _require_workspace(workspace_id)
    body = {"parent_view_id": parent_view_id, "layout": 0, "name": name}
    if markdown:
        body["page_data"] = {"type": "page", "children": _md_to_blocks(markdown)}
    elif page_data:
        body["page_data"] = json.loads(page_data)
    res = _post(f"/api/workspace/{workspace_id}/page-view", body)
    # this endpoint returns {"view_id","database_id"}; the tool contract is the view_id
    return res["view_id"] if isinstance(res, dict) else res


@mcp.tool(annotations=_CREATE)
def create_database(
    workspace_id: str, parent_view_id: str, name: str, layout: str = "grid"
) -> str:
    """Creates a new database page (layout: grid | board | calendar); returns the
    new view_id. Creates default fields — add more with add_database_field, then
    resolve the database_id via list_databases."""
    _require_workspace(workspace_id)
    if layout not in _DB_LAYOUTS:
        raise ValueError("layout must be one of: grid, board, calendar")
    body = {
        "parent_view_id": parent_view_id,
        "layout": _DB_LAYOUTS[layout],
        "name": name,
    }
    res = _post(f"/api/workspace/{workspace_id}/page-view", body)
    return res["view_id"] if isinstance(res, dict) else res


@mcp.tool(annotations=_CREATE)
def create_database_view(
    workspace_id: str, view_id: str, layout: str, name: str = ""
) -> str:
    """Adds another view (grid | board | calendar) over an EXISTING database's
    data. view_id: any existing view of the target database."""
    _require_workspace(workspace_id)
    if layout not in _DB_LAYOUTS:
        raise ValueError("layout must be one of: grid, board, calendar")
    body = {"layout": _DB_LAYOUTS[layout]}
    if name:
        body["name"] = name
    return _post(
        f"/api/workspace/{workspace_id}/page-view/{view_id}/database-view", body
    )


@mcp.tool(annotations=_CREATE)
def add_database_field(
    workspace_id: str,
    database_id: str,
    name: str,
    field_type: int,
    type_option: str = "",
) -> str:
    """Adds a field/column; returns the new field_id. field_type: 0=RichText
    1=Number 2=DateTime 3=SingleSelect 4=MultiSelect 5=Checkbox 6=URL 7=Checklist.
    type_option (optional): JSON string of type-specific options."""
    _require_workspace(workspace_id)
    body = {"name": name, "field_type": field_type}
    if type_option:
        body["type_option_data"] = json.loads(type_option)
    return _post(f"/api/workspace/{workspace_id}/database/{database_id}/fields", body)


@mcp.tool(annotations=_WRITE)
def update_database_field(
    workspace_id: str,
    database_id: str,
    field_id: str,
    name: str = "",
    type_option: str = "",
) -> str:
    """Renames a field/column and/or replaces its raw type-option data (Tier 2 / collab —
    AppFlowy has no REST endpoint for this). name: the new column name. To add/remove
    SingleSelect/MultiSelect OPTIONS use add_select_option / delete_select_option — NOT
    this. type_option (advanced escape hatch): JSON of the field's raw collab type_option
    map, stored verbatim; note select options live as a JSON string at
    type_option["<ty>"]["content"], so a naive options array here will corrupt the field.
    Does NOT convert the field's data type. Only the given attributes change. Returns
    the field_id."""
    _require_workspace(workspace_id)
    if not name and not type_option:
        raise ValueError("provide name and/or type_option to change")
    doc, root = _open_database(workspace_id, database_id)
    fields = root["fields"]
    if field_id not in fields:
        raise ValueError(f"field {field_id} not found in this database")
    sv = doc.get_state()
    with doc.transaction():
        fld = fields[field_id]
        if name:
            fld["name"] = name
        if type_option:
            fld["type_option"] = _to_yjs(json.loads(type_option))
        fld["last_modified"] = int(time.time())
    _collab_web_update(workspace_id, database_id, doc, sv, 1)
    return field_id


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_database_field(workspace_id: str, database_id: str, field_id: str) -> str:
    """Deletes a field/column (Tier 2 / collab — AppFlowy has no REST endpoint for this).
    Removes it from the schema and from every view's column order. The primary/title
    field cannot be deleted. Rows keep the now-orphaned cell harmlessly. There is no
    field trash, so this is irreversible via the API. Returns the deleted field_id."""
    _require_workspace(workspace_id)
    doc, root = _open_database(workspace_id, database_id)
    fields = root["fields"]
    if field_id not in fields:
        raise ValueError(f"field {field_id} not found in this database")
    if "is_primary" in fields[field_id] and fields[field_id]["is_primary"]:
        raise ValueError("the primary (title) field cannot be deleted")
    sv = doc.get_state()
    with doc.transaction():
        del fields[field_id]
        views = root["views"]
        for vid in list(views.keys()):
            view = views[vid]
            if "field_orders" in view:
                orders = view["field_orders"]
                for i in range(len(orders) - 1, -1, -1):
                    if orders[i]["id"] == field_id:
                        del orders[i]
            # ponytail: also drop the per-view setting; a filter/sort/group that
            # referenced this field is left for the client to ignore (rare, tolerated).
            if "field_settings" in view and field_id in view["field_settings"]:
                del view["field_settings"][field_id]
    _collab_web_update(workspace_id, database_id, doc, sv, 1)
    return field_id


@mcp.tool(annotations=_WRITE)
def add_select_option(
    workspace_id: str,
    database_id: str,
    field_id: str,
    name: str,
    color: str = "Purple",
) -> str:
    """Adds an option to a SingleSelect/MultiSelect column (Tier 2 / collab — AppFlowy
    has no REST for it). Returns the option id — pass THAT id (not the label) as the
    cell value in update_row_cells. Idempotent by name: if an option with this name
    already exists its id is returned unchanged. color must be one of Purple, Pink,
    LightPink, Orange, Yellow, Lime, Green, Aqua, Blue, Cream, Mint, Sky, Lilac, Pearl,
    Sunset, Coral, Sapphire, Moss, Sand, Charcoal (default Purple)."""
    _require_workspace(workspace_id)
    if not name:
        raise ValueError("option name is required")
    if color not in SELECT_COLORS:
        raise ValueError(f"color must be one of: {', '.join(SELECT_COLORS)}")
    doc, root = _open_database(workspace_id, database_id)
    fields = root["fields"]
    if field_id not in fields:
        raise ValueError(f"field {field_id} not found in this database")
    field = fields[field_id]
    tk, data = _read_select(field)
    for opt in data["options"]:
        if opt.get("name") == name:
            return opt["id"]  # idempotent — don't create a duplicate
    existing = {o["id"] for o in data["options"]}
    oid = _nid(4)
    while oid in existing:
        oid = _nid(4)
    sv = doc.get_state()
    with doc.transaction():
        data["options"].append({"id": oid, "name": name, "color": color})
        _write_select(field, tk, data)
    _collab_web_update(workspace_id, database_id, doc, sv, 1)
    return oid


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_select_option(
    workspace_id: str, database_id: str, field_id: str, option: str
) -> str:
    """Removes an option from a SingleSelect/MultiSelect column by its option id OR its
    label/name (Tier 2 / collab). Rows still tagged with it keep the now-orphaned id
    harmlessly (AppFlowy ignores unknown option ids). Returns the removed option id."""
    _require_workspace(workspace_id)
    doc, root = _open_database(workspace_id, database_id)
    fields = root["fields"]
    if field_id not in fields:
        raise ValueError(f"field {field_id} not found in this database")
    field = fields[field_id]
    tk, data = _read_select(field)
    match = next(
        (o for o in data["options"] if o["id"] == option or o.get("name") == option),
        None,
    )
    if match is None:
        raise ValueError(f"no option with id or name {option!r} in this field")
    sv = doc.get_state()
    with doc.transaction():
        data["options"] = [o for o in data["options"] if o["id"] != match["id"]]
        _write_select(field, tk, data)
    _collab_web_update(workspace_id, database_id, doc, sv, 1)
    return match["id"]


@mcp.tool(annotations=_WRITE)
def set_group_by(
    workspace_id: str, database_id: str, view_id: str, field_id: str
) -> str:
    """Sets the field a Board view groups its columns by (Tier 2 / collab — AppFlowy has
    no REST for it). view_id: the Board view (from list_databases); field_id: the field
    to group by. A SingleSelect/MultiSelect gives one column per option plus a leading
    "No <field>" column; other groupable types (e.g. Checkbox) regenerate on the client.
    Replaces any existing grouping. Returns the view_id."""
    _require_workspace(workspace_id)
    doc, root = _open_database(workspace_id, database_id)
    views = root["views"]
    if view_id not in views:
        raise ValueError(f"view {view_id} not found in this database")
    fields = root["fields"]
    if field_id not in fields:
        raise ValueError(f"field {field_id} not found in this database")
    ftype = int(fields[field_id]["ty"]) if "ty" in fields[field_id] else 0
    # A select's columns are the field's "no value" group (id == field_id) followed by
    # one group per option (id == option id) — mirror exactly what the client writes so
    # the board renders immediately. Other types are left for the client to regenerate.
    if ftype in _SELECT_TYPES:
        _, sel = _read_select(fields[field_id])
        groups = [{"id": field_id, "visible": True}] + [
            {"id": o["id"], "visible": True} for o in sel["options"]
        ]
    else:
        groups = []
    setting = {
        "id": f"g:{_nid(6)}",
        "field_id": field_id,
        "ty": ftype,
        "content": "",
        "groups": groups,
        "collapsed_group_ids": [],
    }
    sv = doc.get_state()
    with doc.transaction():
        views[view_id]["groups"] = _to_yjs([setting])
    _collab_web_update(workspace_id, database_id, doc, sv, 1)
    return view_id


@mcp.tool(annotations=_CREATE)
def append_blocks(
    workspace_id: str, view_id: str, markdown: str = "", blocks: str = ""
) -> str:
    """Appends content to the END of a document (append-only — cannot edit or
    insert mid-document; use edit_block_text / add_block for that).

    Preferred — `markdown`: rendered into real blocks (same palette as
    create_page). Advanced — `blocks`: a JSON array of block objects
    ({"type":...,"data":...}). Pass either markdown or blocks.

    `view_id` may also be a database ROW id: AppFlowy's REST append-block silently
    ignores those, so rows are transparently re-routed through the collab layer to
    the row's body document (same target as add_block / add_blocks)."""
    _require_workspace(workspace_id)
    if markdown:
        payload = _md_to_blocks(markdown)
    elif blocks:
        payload = json.loads(blocks)
    else:
        raise ValueError("provide `markdown` (preferred) or `blocks`")
    result = _post(
        f"/api/workspace/{workspace_id}/page-view/{view_id}/append-block",
        {"blocks": payload},
    )
    if result:
        return result
    # Empty result: either a legitimately empty success, or `view_id` is a database
    # row id. AppFlowy's append-block endpoint accepts only a real page-view — given
    # a row it returns success and writes NOTHING, so an agent folding a checklist
    # onto a Kanban card got a silent no-op. Only re-route once the id is CONFIRMED
    # to be a row; the REST call provably wrote nothing in that case, so the collab
    # append cannot duplicate content.
    if _is_database_row(workspace_id, view_id):
        return add_blocks(workspace_id, view_id, markdown=markdown, blocks=blocks)
    return result


@mcp.tool(annotations=_CREATE)
def create_space(workspace_id: str, name: str, is_private: bool = False) -> str:
    """Creates a top-level Space; returns its view_id."""
    _require_workspace(workspace_id)
    return _post(
        f"/api/workspace/{workspace_id}/space",
        {
            "name": name,
            "space_permission": 1 if is_private else 0,
            "space_icon": "interface_essential/home-3",
            "space_icon_color": "0xFFA34AFD",
        },
    )


@mcp.tool(annotations=_WRITE)
def move_page(
    workspace_id: str, view_id: str, new_parent_view_id: str, prev_view_id: str = ""
) -> str:
    """Moves a page under new_parent_view_id (prev_view_id: optional sibling to
    place it after)."""
    _require_workspace(workspace_id)
    body = {"new_parent_view_id": new_parent_view_id}
    if prev_view_id:
        body["prev_view_id"] = prev_view_id
    return _post(f"/api/workspace/{workspace_id}/page-view/{view_id}/move", body)


@mcp.tool(annotations=_CREATE)
def duplicate_page(workspace_id: str, view_id: str, suffix: str = "") -> str:
    """Duplicates a page and its subtree."""
    _require_workspace(workspace_id)
    body = {"suffix": suffix} if suffix else {}
    return _post(f"/api/workspace/{workspace_id}/page-view/{view_id}/duplicate", body)


@mcp.tool(annotations=_WRITE)
def trash_page(workspace_id: str, view_id: str) -> str:
    """Moves a page to trash (reversible in-app; there is no hard delete via REST)."""
    _require_workspace(workspace_id)
    return _post(f"/api/workspace/{workspace_id}/page-view/{view_id}/move-to-trash", {})


@mcp.tool(annotations=_WRITE)
def rename_page(workspace_id: str, view_id: str, name: str) -> str:
    """Renames any page, database/board, or space (by its view_id). To retitle a Board
    CARD, set the row's primary cell via update_row_cells instead — a card is a row,
    not a page."""
    _require_workspace(workspace_id)
    return _post(
        f"/api/workspace/{workspace_id}/page-view/{view_id}/update-name",
        {"name": name},
    )


@mcp.tool(annotations=_WRITE)
def restore_page(workspace_id: str, view_id: str) -> str:
    """Restores a page from trash — the inverse of trash_page."""
    _require_workspace(workspace_id)
    return _post(
        f"/api/workspace/{workspace_id}/page-view/{view_id}/restore-from-trash", {}
    )


@mcp.tool(annotations=_READ)
def get_page(workspace_id: str, view_id: str) -> dict:
    """Gets a page's metadata (name, icon, layout, ...). For the page's CONTENT as
    Markdown, use get_page_markdown."""
    _require_workspace(workspace_id)
    path = f"/api/workspace/{workspace_id}/page-view/{view_id}"
    return _api_call("GET", path).json().get("data", {})


@mcp.tool(annotations=_READ)
def get_page_markdown(workspace_id: str, page_id: str) -> str:
    """Reads a Document page — or a database row's body — as Markdown (the inverse
    of create_page(markdown=...)). page_id may be a document view id OR a database
    row id (auto-resolved to the card's body document). Returns Markdown: headings,
    bulleted / numbered / task lists (nested), quotes, fenced code, dividers,
    images, and inline bold / italic / strikethrough / code / links."""
    _require_workspace(workspace_id)
    _, _, document = _open_document(workspace_id, page_id)
    return _doc_to_markdown(document)


# Concurrent collab edits can transiently lose a write, so update_row_cells confirms
# the cells landed (fresh authoritative collab read) and retries — success means it stuck.
_CELL_WRITE_RETRIES = 4
_CELL_WRITE_BACKOFF = 0.4  # seconds, multiplied by the (1-based) attempt number


@mcp.tool(annotations=_WRITE)
def update_row_cells(
    workspace_id: str, database_id: str, row_id: str, cells: str
) -> str:
    """Updates cells on an EXISTING row — works for ANY row, including ones created
    in the UI (Tier 2 / collab). cells: JSON object {field_id: value}. Values:
    text → string; SingleSelect → the option id (see get_database_fields
    type_option options); Checkbox → "Yes"/"No"; Number/URL → string. Only the
    given cells change. To move a Board card, set its Status field's cell.
    Confirms the write applied (read-after-write) before returning and retries on
    transient collab contention, so a success result means the cells actually stuck."""
    _require_workspace(workspace_id)
    updates = json.loads(cells)

    def confirmed(vcells) -> bool:
        for fid, val in updates.items():
            if fid not in vcells or "data" not in vcells[fid]:
                return False
            if str(vcells[fid]["data"]) != str(val):
                return False
        return True

    ftypes = None  # field types for NEW cells — loaded once, only if needed
    last = "no attempt made"
    for attempt in range(_CELL_WRITE_RETRIES):
        doc = _collab_doc(workspace_id, row_id, _ROW_COLLAB)
        row_cells = doc.get("data", type=Map)["data"]["cells"]
        if ftypes is None and any(fid not in row_cells for fid in updates):
            ftypes = _field_types(workspace_id, database_id)
        sv = doc.get_state()
        now = int(time.time())
        with doc.transaction():
            for fid, val in updates.items():
                if fid in row_cells:
                    row_cells[fid]["data"] = val
                    row_cells[fid]["last_modified"] = now
                else:
                    row_cells[fid] = Map(
                        {
                            "data": val,
                            "field_type": int((ftypes or {}).get(fid, 0)),
                            "created_at": now,
                            "last_modified": now,
                        }
                    )
        try:
            _collab_web_update(workspace_id, row_id, doc, sv, _ROW_COLLAB)
            after = _collab_doc(workspace_id, row_id, _ROW_COLLAB).get(
                "data", type=Map
            )["data"]["cells"]
            if confirmed(after):
                return row_id
            last = "cells did not reflect the write on read-back"
        except RuntimeError as e:
            last = str(e)
        if attempt + 1 < _CELL_WRITE_RETRIES:
            time.sleep(_CELL_WRITE_BACKOFF * (attempt + 1))
    raise RuntimeError(
        f"update_row_cells: write to row {row_id} did not confirm after "
        f"{_CELL_WRITE_RETRIES} attempts ({last})"
    )


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_row(workspace_id: str, database_id: str, row_id: str) -> str:
    """Deletes a row from a database — works for ANY row, including UI-created ones
    (Tier 2 / collab). Removes it from every view's row_orders and deletes the
    row's collab object. This is a hard delete (not trash)."""
    _require_workspace(workspace_id)
    doc = _collab_doc(workspace_id, database_id, 1)
    views = doc.get("data", type=Map)["database"]["views"]
    sv = doc.get_state()
    removed = 0
    with doc.transaction():
        for vid in list(views.keys()):
            row_orders = views[vid]["row_orders"]
            for i in reversed(range(len(row_orders))):
                if row_orders[i]["id"] == row_id:
                    del row_orders[i]
                    removed += 1
    if removed:
        _collab_web_update(workspace_id, database_id, doc, sv, 1)
    # Best effort: the row is already gone from every view; a failure here only leaves
    # an orphaned collab object, so don't surface it as a tool error.
    try:
        _api_call(
            "DELETE",
            f"/api/workspace/{workspace_id}/collab/{row_id}",
            json={
                "object_id": row_id,
                "workspace_id": workspace_id,
                "collab_type": _ROW_COLLAB,
            },
        )
    except RuntimeError:
        pass
    return f"deleted row {row_id} (removed from {removed} view order(s))"


@mcp.tool(annotations=_CREATE)
def add_block(
    workspace_id: str,
    page_id: str,
    block_type: str,
    text: str = "",
    data: str = "",
    parent_block_id: str = "",
) -> str:
    """Adds a block to a document (Tier 2 / collab) — including ADVANCED blocks the
    create/markdown paths can't make: callout, toggle_list, quote, heading, code,
    bulleted_list, numbered_list, todo_list, divider, paragraph, etc. Appends to the
    end of the page (or of parent_block_id). Returns the new block id.
    data (optional): JSON of block-specific data, e.g. {"level":2} heading,
    {"icon":"💡"} callout, {"checked":false} todo_list, {"language":"rust"} code.
    page_id = a document's view id, OR a database row id — a row id is auto-resolved
    to the row's body document (this is how you add a checkbox/sub-task to a card).
    `text` renders inline Markdown (bold/italic/code/strike/links); code/math is literal."""
    _require_workspace(workspace_id)
    doc, page_id, d = _open_document(workspace_id, page_id)
    blocks, meta = d["blocks"], d["meta"]
    cmap, tmap = meta["children_map"], meta["text_map"]
    parent = parent_block_id or d["page_id"]
    parent_children_key = blocks[parent]["children"]
    bid, ckey = _nid(), _nid()
    block = {
        "id": bid,
        "ty": block_type,
        "parent": parent,
        "children": ckey,
        "data": data or "{}",
    }
    sv = doc.get_state()
    with doc.transaction():
        cmap[ckey] = Array([])
        if text:
            ext = _nid()
            block["external_id"] = ext
            block["external_type"] = "text"
            tmap[ext] = Text("")
            if block_type in _PLAIN_TEXT_TYS:
                _set_text(tmap[ext], [{"insert": text}])
            else:
                _set_text(tmap[ext], _md_inline_to_delta(text))
        blocks[bid] = Map(block)
        cmap[parent_children_key].append(bid)
    _collab_web_update(workspace_id, page_id, doc, sv, 0)
    return bid


def _insert_pd_block(bmap, cmap, tmap, spec, parent_id) -> str:
    """Insert one page_data block dict (from _md_to_blocks) into a document's collab
    maps, recursively for its children; return the new block id. The inline text
    (data['delta']) moves to the text_map; the rest of data stays on the block. Callers
    append the returned id to the parent's children array. Must run inside a doc
    transaction."""
    bid, ckey = _nid(), _nid()
    data = dict(spec.get("data") or {})
    delta = data.pop("delta", None)
    block = {
        "id": bid,
        "ty": spec["type"],
        "parent": parent_id,
        "children": ckey,
        "data": json.dumps(data),
    }
    cmap[ckey] = Array([])
    if delta is not None:
        ext = _nid()
        block["external_id"] = ext
        block["external_type"] = "text"
        tmap[ext] = Text("")
        _set_text(tmap[ext], delta)
    bmap[bid] = Map(block)
    for child in spec.get("children") or []:
        cmap[ckey].append(_insert_pd_block(bmap, cmap, tmap, child, bid))
    return bid


@mcp.tool(annotations=_WRITE)
def add_blocks(
    workspace_id: str,
    page_id: str,
    markdown: str = "",
    blocks: str = "",
    parent_block_id: str = "",
) -> str:
    """Batch-add MANY blocks to a document or card body in ONE collab round-trip — the
    efficient way to fold a whole checklist or notes section onto a card. Prefer this
    over calling add_block N times (that is N fetch-mutate-post cycles; this is one).
    `markdown`: standard Markdown (headings; nested bulleted / numbered / `- [ ]` task
    lists; quotes; `> [!NOTE]` callouts; fenced code; dividers; images; inline
    bold / italic / code / strike / links) rendered into real blocks. `blocks`: advanced
    — a JSON array of page_data block dicts. Pass exactly one of markdown / blocks.
    page_id = a document view id OR a database row id (auto-resolved to the card's body,
    so this adds sub-tasks to a Kanban card). Appends to the end of the page (or of
    parent_block_id). Returns the new block ids, comma-separated."""
    _require_workspace(workspace_id)
    if markdown:
        specs = _md_to_blocks(markdown)
    elif blocks:
        specs = json.loads(blocks)
    else:
        raise ValueError("add_blocks: provide markdown= or blocks=")
    if not specs:
        return ""
    doc, page_id, d = _open_document(workspace_id, page_id)
    bmap, meta = d["blocks"], d["meta"]
    cmap, tmap = meta["children_map"], meta["text_map"]
    parent = parent_block_id or d["page_id"]
    parent_ckey = bmap[parent]["children"]
    sv = doc.get_state()
    ids = []
    with doc.transaction():
        for spec in specs:
            bid = _insert_pd_block(bmap, cmap, tmap, spec, parent)
            cmap[parent_ckey].append(bid)
            ids.append(bid)
    _collab_web_update(workspace_id, page_id, doc, sv, 0)
    return ",".join(ids)


@mcp.tool(annotations=_WRITE)
def edit_block_text(workspace_id: str, page_id: str, block_id: str, text: str) -> str:
    """Replaces the text of an existing document block. `text` renders inline Markdown
    (**bold**, *italic*, `code`, ~~strike~~, [links](url)); code/math blocks keep it
    literal. page_id may be a document view id or a database row id (auto-resolved)."""
    _require_workspace(workspace_id)
    doc, page_id, d = _open_document(workspace_id, page_id)
    block = d["blocks"][block_id]
    tmap = d["meta"]["text_map"]
    ext = block.get("external_id", None)
    ty = block.get("ty", "")
    delta = [{"insert": text}] if ty in _PLAIN_TEXT_TYS else _md_inline_to_delta(text)
    sv = doc.get_state()
    with doc.transaction():
        if not (ext and ext in tmap):
            ext = _nid()
            block["external_id"] = ext
            block["external_type"] = "text"
            tmap[ext] = Text("")
        _set_text(tmap[ext], delta)
    _collab_web_update(workspace_id, page_id, doc, sv, 0)
    return block_id


@mcp.tool(annotations=_WRITE)
def replace_text(
    workspace_id: str,
    page_id: str,
    find: str,
    replace: str = "",
    replace_all: bool = False,
) -> str:
    """Find-and-replace text in a document WITHOUT needing block ids — the
    content-addressed edit (like Notion's update_content). Every occurrence of `find`
    within a block's text becomes `replace` (plain text). page_id may be a document
    view id or a database row id (card body). By default errors if `find` appears in
    more than one block; set replace_all=true to change all of them. Returns how many
    blocks changed."""
    _require_workspace(workspace_id)
    if not find:
        raise ValueError("`find` must be non-empty")
    doc, page_id, d = _open_document(workspace_id, page_id)
    blocks, tmap = d["blocks"], d["meta"]["text_map"]
    hits = []
    for bid in blocks:
        b = blocks[bid]
        ext = b.get("external_id", None)
        if ext is not None and ext in tmap and find in str(tmap[ext]):
            hits.append(ext)
    if not hits:
        raise ValueError(f"`find` text not found: {find!r}")
    if len(hits) > 1 and not replace_all:
        raise ValueError(
            f"`find` matches {len(hits)} blocks — narrow it or set replace_all=true"
        )
    sv = doc.get_state()
    with doc.transaction():
        for ext in hits:
            t = tmap[ext]
            s = str(t)
            starts, i = [], s.find(find)
            while i != -1:
                starts.append(i)
                i = s.find(find, i + len(find))
            for i in reversed(starts):
                del t[i : i + len(find)]
                if replace:
                    t.insert(i, replace)
    _collab_web_update(workspace_id, page_id, doc, sv, 0)
    return f"replaced in {len(hits)} block(s)"


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_block(workspace_id: str, page_id: str, block_id: str) -> str:
    """Deletes a block from a document (removes it from its parent, plus its text
    and children references). page_id may be a document view id or a row id."""
    _require_workspace(workspace_id)
    doc, page_id, d = _open_document(workspace_id, page_id)
    blocks, cmap, tmap = d["blocks"], d["meta"]["children_map"], d["meta"]["text_map"]
    if block_id not in blocks:
        raise ValueError(f"block {block_id} not found")
    block = blocks[block_id]
    parent = block.get("parent", "")
    ext = block.get("external_id", None)
    ckey = block.get("children", None)
    sv = doc.get_state()
    with doc.transaction():
        if parent and parent in blocks:
            pkey = blocks[parent]["children"]
            if pkey in cmap:
                arr = cmap[pkey]
                for i in reversed(range(len(arr))):
                    if arr[i] == block_id:
                        del arr[i]
        if ext and ext in tmap:
            del tmap[ext]
        if ckey and ckey in cmap:
            del cmap[ckey]
        del blocks[block_id]
    _collab_web_update(workspace_id, page_id, doc, sv, 0)
    return f"deleted block {block_id}"


# ---- HTTP app (Streamable HTTP + SSE), Bearer-gated -------------------------

_streamable_app = mcp.streamable_http_app()
_sse_app = mcp.sse_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Streamable HTTP needs its session manager running for the app's lifetime.
    async with mcp.session_manager.run():
        yield


class _RedactTokenFilter(logging.Filter):
    """Keep `?token=` out of the access log.

    Clients that cannot set an Authorization header (the Claude connector dialog
    only takes a URL) must pass the shared secret as a query parameter. Uvicorn's
    access logger writes the full request line, so without this every such request
    prints a working credential to stdout — where `docker logs` hands it to anyone
    who can read them, and log shippers happily archive it forever. Observed in the
    wild, not hypothetical.

    Belt-and-braces: prefer the Authorization header wherever the client supports
    it. Redaction limits the blast radius; it does not make a URL secret.
    """

    _RE = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._RE.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._RE.sub(r"\1[REDACTED]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())

app = FastAPI(title="AppFlowy MCP Server", lifespan=lifespan)


_PUBLIC_PREFIXES = (
    "/robots.txt",
    "/icon.svg",
    "/.well-known/",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
    "/auth/google/",
)


@app.middleware("http")
async def verify_token(request: Request, call_next):
    path = request.url.path
    public = any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)
    secret = os.environ.get("MCP_SECRET_TOKEN")
    if not public and secret:
        auth = request.headers.get("Authorization", "")
        # Bearer header (technical clients) or ?token= (link method for UIs that
        # can't set a header — the token then rides in the URL/logs).
        token = (
            auth[7:]
            if auth.startswith("Bearer ")
            else request.query_params.get("token", "")
        )
        ok = bool(token) and hmac.compare_digest(token, secret)
        if not ok and _oauth_provider and token:
            ok = bool(await _oauth_provider.load_access_token(token))
        if not ok:
            resp = JSONResponse(
                status_code=401,
                content={
                    "detail": "Unauthorized. Use a Bearer token, ?token=, or OAuth sign-in."
                },
            )
            if _oauth_provider:
                resp.headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{OAUTH_ISSUER}'
                    '/.well-known/oauth-protected-resource"'
                )
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
            return resp

    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/icon.svg")
async def icon_svg():
    return Response(ICON_SVG, media_type="image/svg+xml")


if _oauth_provider:
    from mcp.server.auth.routes import (
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    _issuer = AnyHttpUrl(OAUTH_ISSUER)
    app.router.routes.extend(
        create_auth_routes(
            _oauth_provider,
            _issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["appflowy"], default_scopes=["appflowy"]
            ),
        )
    )
    app.router.routes.extend(
        create_protected_resource_routes(
            resource_url=_issuer,
            authorization_servers=[_issuer],
            scopes_supported=["appflowy"],
            resource_name="AppFlowy MCP",
        )
    )

    @app.get("/auth/google/callback")
    async def google_callback(request: Request):
        return await _oauth_provider.handle_google_callback(
            request.query_params.get("code", ""),
            request.query_params.get("state", ""),
        )


app.mount("/mcp", _streamable_app)
app.mount("/sse", _sse_app)


if __name__ == "__main__":
    # Run directly for local use over stdio.
    mcp.run(transport="stdio")
