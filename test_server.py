"""Smoke tests for the security-critical logic: Bearer auth gate, workspace
guard, and that both HTTP transports boot cleanly."""

import os

import pytest

os.environ.setdefault("MCP_SECRET_TOKEN", "test-secret-123")
os.environ.setdefault("ALLOWED_WORKSPACE_IDS", "ws-allowed")
os.environ.setdefault("MCP_ALLOWED_HOSTS", "testserver")

from fastapi.testclient import TestClient

import server


def test_agent_guide_resource():
    # The full guide is served as an MCP resource; the file must resolve + load.
    guide = server._agent_guide_md()
    assert "AppFlowy Agent Guide" in guide and "Coverage" in guide


def test_access_log_redacts_query_token():
    # The ?token= link method puts a working credential in the request line, which
    # uvicorn's access logger writes verbatim to stdout. Anything that can read
    # `docker logs` then has full access, so the secret must never reach the log.
    import logging

    f = server._RedactTokenFilter()

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        None,
        None,
    )
    rec.args = ("1.2.3.4:0", "POST", "/mcp/?token=supersecretvalue", "1.1", 200)
    f.filter(rec)
    assert "supersecretvalue" not in str(rec.args)
    assert "token=[REDACTED]" in str(rec.args)

    # Non-token args must pass through untouched.
    assert "/mcp/" in str(rec.args) and "POST" in str(rec.args)

    # Also scrubbed when the secret is baked into the message itself.
    rec2 = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "GET /sse?token=abc123&x=1",
        None,
        None,
    )
    f.filter(rec2)
    assert "abc123" not in rec2.msg
    assert "x=1" in rec2.msg  # other query params are preserved


def test_dollar_amounts_survive_markdown_parsing():
    # Regression: with dollarmath's defaults, a sentence with two dollar AMOUNTS had
    # everything between them swallowed into a math node —
    #   "ARR hit $560k this quarter, up from $380k last year."
    #     -> "ARR hit 380k last year."
    # Silent, unrecoverable loss of figures in ordinary prose.
    money = "ARR hit $560k this quarter, up from $380k last year."
    blocks = server._md_to_blocks(money)
    rendered = " ".join(
        seg.get("insert", "")
        for b in blocks
        for seg in (b.get("data", {}) or {}).get("delta", [])
    )
    assert "$560k" in rendered
    assert "$380k" in rendered
    assert "this quarter, up from" in rendered  # the text between them survives


def test_inline_math_still_parses():
    # The money fix must not cost us real math: a TeX expression with no digit or
    # space against the delimiter is still math, and $$...$$ blocks are untouched.
    toks = server._md_parser.parse("Euler: $e^{i\\pi}+1=0$ done")
    kinds = {c.type for t in toks if t.type == "inline" for c in (t.children or [])}
    assert "math_inline" in kinds

    blocks = server._md_to_blocks("$$\nx^2 + y^2 = z^2\n$$")
    assert any(b.get("type") == "math_equation" for b in blocks)


def test_collab_doc_state_tolerates_shapes():
    import base64

    b = server._collab_doc_state
    # old envelope: {"data": {"doc_state": [ints]}}
    assert b({"data": {"doc_state": [1, 2, 3]}}) == bytes([1, 2, 3])
    # newer: encoded collab nested, envelope stripped
    assert b({"encode_collab": {"doc_state": [4, 5]}}) == bytes([4, 5])
    # newer, still wrapped: envelope + nested encode_collab
    assert b({"data": {"encode_collab": {"doc_state": [6]}}}) == bytes([6])
    # base64-string doc_state (newer builds)
    assert b({"data": {"doc_state": base64.b64encode(b"yjs").decode()}}) == b"yjs"
    # unknown shape -> loud error that names the keys (never a bare KeyError)
    with pytest.raises(RuntimeError, match="doc_state"):
        b({"weird": {"nope": 1}})


def test_add_blocks_batch_insert():
    # add_blocks folds a whole Markdown checklist into a card in one pass; verify the
    # block-tree inserter lands every block by rendering the doc back to Markdown.
    from pycrdt import Array, Doc, Map

    doc = Doc()
    root = doc.get("data", type=Map)
    with doc.transaction():
        root["document"] = Map(
            {
                "blocks": Map(
                    {"page": Map({"ty": "page", "data": "{}", "children": "pc"})}
                ),
                "meta": Map(
                    {"children_map": Map({"pc": Array([])}), "text_map": Map()}
                ),
                "page_id": "page",
            }
        )
        d = root["document"]
        bmap, cmap, tmap = d["blocks"], d["meta"]["children_map"], d["meta"]["text_map"]
        specs = server._md_to_blocks(
            "## Plan\n- [ ] first item\n- [ ] **bold** second\n\nplain note"
        )
        for spec in specs:
            cmap["pc"].append(server._insert_pd_block(bmap, cmap, tmap, spec, "page"))
    md = server._doc_to_markdown(d)
    assert "## Plan" in md
    assert "- [ ] first item" in md
    assert "- [ ] **bold** second" in md
    assert "plain note" in md
    assert len(list(cmap["pc"])) == 4  # heading + 2 todos + paragraph, one fold


def test_set_text_utf8_offsets_with_emoji():
    # pycrdt Text indexes by UTF-8 byte; a leading emoji (4 bytes) must not drift the
    # format range, or links/bold after it land on the wrong characters.
    from pycrdt import Doc, Text

    d = Doc()
    t = d.get("t", type=Text)
    with d.transaction():
        server._set_text(
            t,
            [
                {"insert": "🔗 see "},
                {"insert": "PREP-380", "attributes": {"href": "http://x"}},
                {"insert": " now"},
            ],
        )
    assert t.diff() == [
        ("🔗 see ", None),
        ("PREP-380", {"href": "http://x"}),
        (" now", None),
    ]


def test_workspace_guard_blocks_other_workspaces():
    server._require_workspace("ws-allowed")  # allowed -> no raise
    with pytest.raises(ValueError):
        server._require_workspace("ws-other")


def test_workspace_guard_unrestricted_when_unset(monkeypatch):
    monkeypatch.delenv("ALLOWED_WORKSPACE_IDS", raising=False)
    assert server._allowed_workspaces() is None
    server._require_workspace("anything")  # unrestricted -> no raise


def test_auth_middleware_and_transports_boot():
    # TestClient context manager runs lifespan -> proves the Streamable HTTP
    # session manager (and both mounts) start without error.
    with TestClient(server.app) as client:
        assert client.get("/robots.txt").status_code == 200  # always open
        assert client.get("/nope").status_code == 401  # no token
        assert (
            client.get("/nope", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )  # wrong token
        # correct token passes the auth gate (404 from routing, not 401)
        assert (
            client.get(
                "/nope", headers={"Authorization": "Bearer test-secret-123"}
            ).status_code
            == 404
        )
        # link method: token as ?token= query param
        assert client.get("/nope?token=test-secret-123").status_code == 404
        assert client.get("/nope?token=wrong").status_code == 401


def test_row_document_id_derivation():
    # A database row's body document is a separate collab at
    # uuid5(row_uuid, "document_id"); add_block/edit_block_text/delete_block
    # resolve a row id to it. Locked against a synthetic id.
    assert (
        server._row_document_id("11111111-1111-1111-1111-111111111111")
        == "f972a45d-1193-586f-99a2-89f5406db9fc"
    )


def test_to_yjs_wraps_nested_json():
    # pycrdt nodes can only be read once integrated into a doc, which is exactly how
    # the tool uses _to_yjs (assigned straight into the field). Mirror that here.
    from pycrdt import Array, Doc, Map

    doc = Doc()
    m = doc.get("t", type=Map)
    with doc.transaction():
        m["v"] = server._to_yjs({"3": {"options": [{"id": "o1", "name": "Open"}]}})
    to = m["v"]
    assert isinstance(to, Map) and isinstance(to["3"], Map)
    assert isinstance(to["3"]["options"], Array)
    assert to["3"]["options"][0]["name"] == "Open"


def _synthetic_db_doc():
    """A database collab shaped like AppFlowy's: data.database.{fields,views}."""
    from pycrdt import Array, Doc, Map

    doc = Doc()
    data = doc.get("data", type=Map)
    with doc.transaction():
        data["database"] = Map(
            {
                "fields": Map(
                    {
                        "F1": Map({"id": "F1", "name": "Title", "is_primary": True}),
                        "F2": Map({"id": "F2", "name": "Status", "ty": 3}),
                    }
                ),
                "views": Map(
                    {
                        "V1": Map(
                            {
                                "field_orders": Array(
                                    [Map({"id": "F1"}), Map({"id": "F2"})]
                                ),
                                "field_settings": Map(
                                    {"F1": Map({"w": 1}), "F2": Map({"w": 2})}
                                ),
                            }
                        )
                    }
                ),
            }
        )
    return doc


def test_field_update_and_delete_mutate_collab(monkeypatch):
    # Exercise the real tools against a synthetic doc with the network stubbed out.
    from pycrdt import Map

    doc = _synthetic_db_doc()
    monkeypatch.setattr(server, "_collab_doc", lambda ws, oid, ct: doc)
    monkeypatch.setattr(server, "_collab_web_update", lambda *a, **k: None)
    root = doc.get("data", type=Map)["database"]

    server.update_database_field("ws-allowed", "db", "F2", name="State")
    assert root["fields"]["F2"]["name"] == "State"

    server.delete_database_field("ws-allowed", "db", "F2")
    assert "F2" not in root["fields"]
    assert [o["id"] for o in list(root["views"]["V1"]["field_orders"])] == ["F1"]
    assert "F2" not in root["views"]["V1"]["field_settings"]

    with pytest.raises(ValueError):  # primary/title field is protected
        server.delete_database_field("ws-allowed", "db", "F1")
    with pytest.raises(ValueError):  # unknown field id
        server.update_database_field("ws-allowed", "db", "NOPE", name="x")


def test_select_option_add_and_delete(monkeypatch):
    # F2 is a SingleSelect (ty=3) with no options yet.
    import json

    from pycrdt import Map

    doc = _synthetic_db_doc()
    monkeypatch.setattr(server, "_collab_doc", lambda ws, oid, ct: doc)
    monkeypatch.setattr(server, "_collab_web_update", lambda *a, **k: None)
    root = doc.get("data", type=Map)["database"]

    oid = server.add_select_option("ws-allowed", "db", "F2", "Blocked", color="Blue")
    assert isinstance(oid, str) and len(oid) == 4
    assert (
        server.add_select_option("ws-allowed", "db", "F2", "Blocked") == oid
    )  # idempotent

    # Options persist as a JSON STRING under type_option["3"]["content"].
    data = json.loads(root["fields"]["F2"]["type_option"]["3"]["content"])
    assert [o["name"] for o in data["options"]] == ["Blocked"]
    assert data["options"][0]["color"] == "Blue"

    with pytest.raises(ValueError):  # bad color would wipe options in AppFlowy
        server.add_select_option("ws-allowed", "db", "F2", "X", color="Neon")
    with pytest.raises(ValueError):  # F1 is not a select column
        server.add_select_option("ws-allowed", "db", "F1", "X")

    assert server.delete_select_option("ws-allowed", "db", "F2", "Blocked") == oid
    data = json.loads(root["fields"]["F2"]["type_option"]["3"]["content"])
    assert data["options"] == []
    with pytest.raises(ValueError):  # no such option
        server.delete_select_option("ws-allowed", "db", "F2", "ghost")


def test_set_group_by(monkeypatch):
    from pycrdt import Map

    doc = _synthetic_db_doc()
    monkeypatch.setattr(server, "_collab_doc", lambda ws, oid, ct: doc)
    monkeypatch.setattr(server, "_collab_web_update", lambda *a, **k: None)
    root = doc.get("data", type=Map)["database"]

    oid = server.add_select_option(
        "ws-allowed", "db", "F2", "Open"
    )  # F2 is SingleSelect
    assert server.set_group_by("ws-allowed", "db", "V1", "F2") == "V1"

    gs = root["views"]["V1"]["groups"][0]
    assert gs["field_id"] == "F2" and gs["ty"] == 3
    # columns = the "no value" group (field_id) then one per option
    assert [g["id"] for g in list(gs["groups"])] == ["F2", oid]

    with pytest.raises(ValueError):  # unknown view
        server.set_group_by("ws-allowed", "db", "NOPE", "F2")
    with pytest.raises(ValueError):  # unknown field
        server.set_group_by("ws-allowed", "db", "V1", "NOPE")


def _synthetic_row_doc(data0="old"):
    from pycrdt import Doc, Map

    doc = Doc()
    root = doc.get("data", type=Map)
    with doc.transaction():
        root["data"] = Map(
            {"cells": Map({"F1": Map({"data": data0, "field_type": 0})})}
        )
    return doc


def test_update_row_cells_confirms_and_creates_new_cell(monkeypatch):
    from pycrdt import Map

    doc = _synthetic_row_doc()
    monkeypatch.setattr(server, "_collab_doc", lambda ws, oid, ct: doc)
    monkeypatch.setattr(server, "_collab_web_update", lambda *a, **k: None)
    monkeypatch.setattr(server, "_field_types", lambda ws, db: {"F2": 3})
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    out = server.update_row_cells(
        "ws-allowed", "db", "row1", '{"F1": "new", "F2": "opt1"}'
    )
    assert out == "row1"
    cells = doc.get("data", type=Map)["data"]["cells"]
    assert cells["F1"]["data"] == "new"
    # a brand-new cell is tagged with the type from _field_types (the collab), not 0
    assert cells["F2"]["data"] == "opt1" and cells["F2"]["field_type"] == 3


def test_update_row_cells_raises_when_write_never_confirms(monkeypatch):
    from pycrdt import Map

    doc = _synthetic_row_doc()
    monkeypatch.setattr(server, "_collab_doc", lambda ws, oid, ct: doc)

    def revert(*a, **k):  # simulate a write that doesn't actually stick
        with doc.transaction():
            doc.get("data", type=Map)["data"]["cells"]["F1"]["data"] = "old"

    monkeypatch.setattr(server, "_collab_web_update", revert)
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="did not confirm"):
        server.update_row_cells("ws-allowed", "db", "row1", '{"F1": "new"}')


def _mock_http(monkeypatch, handler):
    """Point server.httpx.Client at a MockTransport and stub auth.

    NOTE: `server.httpx` IS the global httpx module, so patching `.Client` here is a
    global patch. Capture the pristine class from the *class* object rather than the
    module attribute, or a second call in the same test would wrap the first mock
    (and keep serving the first handler) instead of replacing it.
    """
    import httpx

    real_client = _PRISTINE_HTTPX_CLIENT
    monkeypatch.setattr(
        server.httpx,
        "Client",
        lambda **k: real_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        server, "get_auth_headers", lambda: {"Authorization": "Bearer x"}
    )


def _pristine_httpx_client():
    import httpx

    return httpx.Client


_PRISTINE_HTTPX_CLIENT = _pristine_httpx_client()


def test_is_database_row_discriminates_row_from_document(monkeypatch):
    # append_blocks routes on this: AppFlowy's page-view payload carries `row_data`
    # for a database row and `encoded_collab` for a document.
    import httpx

    def as_row(_request):
        return httpx.Response(200, json={"data": {"data": {"row_data": {}}}})

    _mock_http(monkeypatch, as_row)
    assert server._is_database_row("ws", "some-id") is True

    def as_doc(_request):
        return httpx.Response(200, json={"data": {"data": {"encoded_collab": "..."}}})

    _mock_http(monkeypatch, as_doc)
    assert server._is_database_row("ws", "some-id") is False

    # Flat shape (AppFlowy has moved this key around across releases).
    def as_row_flat(_request):
        return httpx.Response(200, json={"data": {"row_data": {}}})

    _mock_http(monkeypatch, as_row_flat)
    assert server._is_database_row("ws", "some-id") is True


def test_is_database_row_never_raises(monkeypatch):
    # It only ever ADDS a fallback path, so an error must answer False and leave the
    # caller's existing behaviour untouched — never turn a probe into a failure.
    import httpx

    def boom(_request):
        return httpx.Response(500, text="upstream exploded")

    _mock_http(monkeypatch, boom)
    assert server._is_database_row("ws", "some-id") is False


def test_append_blocks_reroutes_row_id_to_collab(monkeypatch):
    # The bug: AppFlowy's REST append-block accepts only a real page-view. Handed a
    # database ROW id it returns success and writes NOTHING, so folding a checklist
    # onto a Kanban card silently lost the content.
    calls = {}

    def fake_add_blocks(ws, pid, markdown="", blocks=""):
        calls["md"] = markdown
        calls["page_id"] = pid
        return "blk1"

    monkeypatch.setattr(server, "_require_workspace", lambda *_: None)
    monkeypatch.setattr(server, "_post", lambda *a, **k: "")  # the silent no-op
    monkeypatch.setattr(server, "_is_database_row", lambda *_: True)
    monkeypatch.setattr(server, "add_blocks", fake_add_blocks)

    out = server.append_blocks("ws", "row-id", markdown="- [ ] fold me")
    assert out == "blk1"  # delegated to the collab path
    assert calls["md"] == "- [ ] fold me"  # content carried across intact
    assert calls["page_id"] == "row-id"  # and aimed at the row, not the page


def test_append_blocks_leaves_document_path_untouched(monkeypatch):
    # Zero-regression guard: a normal page-view append must not gain an extra probe
    # or a second write just because the row fallback exists.
    seen = {"posts": 0, "probes": 0}

    def fake_post(*_a, **_k):
        seen["posts"] += 1
        return "ok-block-id"

    def fake_probe(*_a, **_k):
        seen["probes"] += 1
        return True

    monkeypatch.setattr(server, "_require_workspace", lambda *_: None)
    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_is_database_row", fake_probe)

    assert server.append_blocks("ws", "page-id", markdown="hello") == "ok-block-id"
    assert seen["posts"] == 1  # exactly one write
    assert seen["probes"] == 0  # no extra round-trip on the happy path


def test_api_call_actionable_error(monkeypatch):
    # A non-2xx from AppFlowy must surface as a specific, agent-readable message
    # (not a bare traceback), with a hint about what to fix.
    import httpx

    def handler(_request):
        return httpx.Response(404, text="object not found")

    real_client = httpx.Client  # capture before patching to avoid recursion
    monkeypatch.setattr(
        server.httpx,
        "Client",
        lambda **k: real_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        server, "get_auth_headers", lambda: {"Authorization": "Bearer x"}
    )

    with pytest.raises(RuntimeError) as ei:
        server._api_call("GET", "/api/workspace/x/database/y/fields")
    msg = str(ei.value)
    assert "AppFlowy API 404" in msg
    assert "database_id" in msg  # the 404 hint steers toward list_databases
    assert "object not found" in msg  # includes the server's own words


def test_oauth_store_persists_across_instances(tmp_path):
    # Tokens must survive a restart: a fresh provider pointed at the same store
    # file reloads what a prior instance saved (this is what stops re-sign-in).
    from mcp.server.auth.provider import AccessToken

    from google_oauth import GoogleOAuthProvider

    path = str(tmp_path / "oauth.json")
    p = GoogleOAuthProvider(
        "https://mcp.example.com", "cid", "sec", ["a@b.com"], store_path=path
    )
    p.access["tok1"] = AccessToken(
        token="tok1", client_id="c1", scopes=["appflowy"], expires_at=9999999999
    )
    p._save()

    p2 = GoogleOAuthProvider(
        "https://mcp.example.com", "cid", "sec", ["a@b.com"], store_path=path
    )
    assert "tok1" in p2.access
    assert p2.access["tok1"].client_id == "c1"


def test_md_to_blocks_covers_core_palette():
    # The Markdown content interface: one string -> the AppFlowy block tree
    # create_page / append_blocks feed to AppFlowy.
    md = (
        "# Title\n\n"
        "Some **bold**, *italic*, `code`, ~~struck~~, and [a link](http://x.com).\n\n"
        "- one\n"
        "- two\n"
        "  - nested\n"
        "- [ ] todo\n"
        "- [x] done\n\n"
        "1. first\n"
        "2. second\n\n"
        "> quoted line\n\n"
        '```python\nprint("hi")\n```\n\n'
        "---\n\n"
        "![pic](http://img/p.png)\n"
    )
    blocks = server._md_to_blocks(md)

    assert blocks[0] == {
        "type": "heading",
        "data": {"level": 1, "delta": [{"insert": "Title"}]},
    }
    # inline formatting in the paragraph
    ops = next(b for b in blocks if b["type"] == "paragraph")["data"]["delta"]
    assert {"insert": "bold", "attributes": {"bold": True}} in ops
    assert {"insert": "italic", "attributes": {"italic": True}} in ops
    assert {"insert": "code", "attributes": {"code": True}} in ops
    assert {"insert": "struck", "attributes": {"strikethrough": True}} in ops
    assert any(o.get("attributes", {}).get("href") == "http://x.com" for o in ops)
    # lists: nesting + GFM task state (checked/unchecked, marker stripped)
    assert any(b["type"] == "bulleted_list" and "children" in b for b in blocks)
    todos = [b for b in blocks if b["type"] == "todo_list"]
    assert [t["data"]["checked"] for t in todos] == [False, True]
    assert [t["data"]["delta"][0]["insert"] for t in todos] == ["todo", "done"]
    assert any(b["type"] == "numbered_list" for b in blocks)
    # quote, code (with language), divider, image block
    assert any(b["type"] == "quote" for b in blocks)
    code = next(b for b in blocks if b["type"] == "code")
    assert code["data"]["language"] == "python"
    assert code["data"]["delta"][0]["insert"] == 'print("hi")'
    assert any(b["type"] == "divider" for b in blocks)
    img = next(b for b in blocks if b["type"] == "image")
    assert img["data"]["url"] == "http://img/p.png"


def test_md_to_blocks_empty_and_fallback():
    assert server._md_to_blocks("") == []
    assert server._md_to_blocks("   \n") == []
    # an unsupported construct (GFM table) degrades to plaintext, never dropped
    blocks = server._md_to_blocks("| a | b |\n|---|---|\n| 1 | 2 |")
    assert blocks and all(b["type"] == "paragraph" for b in blocks)
    assert (
        "".join(o["insert"] for b in blocks for o in b["data"]["delta"]).strip() != ""
    )


def test_inline_md_formatting():
    f = server._inline_md
    assert f([("plain", None)]) == "plain"
    assert f([("b", {"bold": True})]) == "**b**"
    assert f([("i", {"italic": True})]) == "*i*"
    assert f([("s", {"strikethrough": True})]) == "~~s~~"
    assert f([("c", {"code": True})]) == "`c`"
    assert f([("x", {"href": "http://u"})]) == "[x](http://u)"
    assert f([("x", {"bold": True, "italic": True})]) == "***x***"
    assert f([("x", {"bold": True, "href": "http://u"})]) == "[**x**](http://u)"


def test_doc_to_markdown_renders_blocks():
    # Mirrors AppFlowy's real document collab: block.data holds type fields, text
    # lives in text_map as a yjs Text whose diff() yields the delta.
    from pycrdt import Array, Doc, Map, Text

    doc = Doc()
    root = doc.get("data", type=Map)
    with doc.transaction():
        root["document"] = Map(
            {
                "blocks": Map(),
                "meta": Map({"children_map": Map(), "text_map": Map()}),
                "page_id": "page",
            }
        )
        document = root["document"]
        blocks = document["blocks"]
        cmap = document["meta"]["children_map"]
        tmap = document["meta"]["text_map"]

        def add(bid, ty, data, ckey, text=None):
            m = {"ty": ty, "data": data, "children": ckey}
            if text is not None:
                ext = "x" + bid
                m["external_id"] = ext
                tmap[ext] = Text(text)
            blocks[bid] = Map(m)

        add("page", "page", "{}", "pc")
        cmap["pc"] = Array(["h", "b1", "b2", "t1", "c1"])
        add("h", "heading", '{"level":2}', "hc", "Title")
        cmap["hc"] = Array([])
        add("b1", "bulleted_list", "{}", "b1c", "parent")
        cmap["b1c"] = Array(["b1n"])
        add("b1n", "bulleted_list", "{}", "b1nc", "child")
        cmap["b1nc"] = Array([])
        add("b2", "bulleted_list", "{}", "b2c", "sibling")
        cmap["b2c"] = Array([])
        add("t1", "todo_list", '{"checked":true}', "t1c", "done")
        cmap["t1c"] = Array([])
        add("c1", "code", '{"language":"py"}', "c1c", "x=1\ny=2")
        cmap["c1c"] = Array([])
    md = server._doc_to_markdown(root["document"])
    assert "## Title" in md
    assert "- parent\n  - child" in md  # nested item indented under its parent
    assert "- sibling" in md
    assert "- [x] done" in md
    assert "```py\nx=1\ny=2\n```" in md


def test_md_inline_and_set_text():
    from pycrdt import Doc, Map, Text

    delta = server._md_inline_to_delta("a **b** ~~c~~ [x](http://u) `k`")
    assert {"insert": "b", "attributes": {"bold": True}} in delta
    assert {"insert": "c", "attributes": {"strikethrough": True}} in delta
    assert {"insert": "k", "attributes": {"code": True}} in delta
    assert any(o.get("attributes", {}).get("href") == "http://u" for o in delta)

    doc = Doc()
    m = doc.get("t", type=Map)
    with doc.transaction():
        m["x"] = Text("stale")
        server._set_text(m["x"], delta)  # overwrites, applies formatting ranges
    diff = m["x"].diff()
    assert "".join(seg for seg, _ in diff) == "a b c x k"
    assert ("b", {"bold": True}) in diff
    assert ("k", {"code": True}) in diff


def test_replace_text(monkeypatch):
    from pycrdt import Array, Doc, Map, Text

    doc = Doc()
    root = doc.get("data", type=Map)
    with doc.transaction():
        root["document"] = Map(
            {
                "blocks": Map(),
                "meta": Map({"children_map": Map(), "text_map": Map()}),
                "page_id": "page",
            }
        )
        d = root["document"]
        blocks = d["blocks"]
        tmap = d["meta"]["text_map"]
        cmap = d["meta"]["children_map"]
        blocks["page"] = Map({"ty": "page", "children": "pc"})
        cmap["pc"] = Array(["p1", "p2"])
        tmap["t1"] = Text("hello world")
        blocks["p1"] = Map({"ty": "paragraph", "children": "c1", "external_id": "t1"})
        cmap["c1"] = Array([])
        tmap["t2"] = Text("goodbye world")
        blocks["p2"] = Map({"ty": "paragraph", "children": "c2", "external_id": "t2"})
        cmap["c2"] = Array([])

    monkeypatch.setattr(
        server, "_open_document", lambda ws, pid: (doc, pid, root["document"])
    )
    monkeypatch.setattr(server, "_collab_web_update", lambda *a, **k: None)

    server.replace_text("ws-allowed", "page", "hello", "hi")
    assert str(tmap["t1"]) == "hi world"
    with pytest.raises(ValueError):  # "world" is in two blocks
        server.replace_text("ws-allowed", "page", "world", "earth")
    server.replace_text("ws-allowed", "page", "world", "earth", replace_all=True)
    assert str(tmap["t1"]) == "hi earth"
    assert str(tmap["t2"]) == "goodbye earth"
    with pytest.raises(ValueError):  # not found
        server.replace_text("ws-allowed", "page", "zzz", "x")


def test_callout_and_math_blocks():
    blocks = server._md_to_blocks("> [!WARNING]\n> Be careful here.\n\n$$\nE=mc^2\n$$")
    callout = next(b for b in blocks if b["type"] == "callout")
    assert callout["data"]["icon"] == "⚠️"
    assert callout["data"]["delta"] == [{"insert": "Be careful here."}]
    math = next(b for b in blocks if b["type"] == "math_equation")
    assert math["data"]["formula"] == "E=mc^2"
    # inline math survives as literal $...$
    para = server._md_to_blocks("mass $m$ energy")[0]
    assert "$m$" in "".join(o["insert"] for o in para["data"]["delta"])


def test_doc_to_markdown_callout_math_toggle():
    from pycrdt import Array, Doc, Map, Text

    doc = Doc()
    root = doc.get("data", type=Map)
    with doc.transaction():
        root["document"] = Map(
            {
                "blocks": Map(),
                "meta": Map({"children_map": Map(), "text_map": Map()}),
                "page_id": "page",
            }
        )
        d = root["document"]
        blocks = d["blocks"]
        tmap = d["meta"]["text_map"]
        cmap = d["meta"]["children_map"]
        blocks["page"] = Map({"ty": "page", "children": "pc"})
        cmap["pc"] = Array(["co", "mq", "tg"])
        tmap["c"] = Text("Heads up")
        blocks["co"] = Map(
            {
                "ty": "callout",
                "data": '{"icon":"⚠️"}',
                "external_id": "c",
                "children": "cc",
            }
        )
        cmap["cc"] = Array([])
        blocks["mq"] = Map(
            {"ty": "math_equation", "data": '{"formula":"a^2+b^2"}', "children": "mc"}
        )
        cmap["mc"] = Array([])
        tmap["t"] = Text("Details")
        blocks["tg"] = Map(
            {
                "ty": "toggle_list",
                "data": '{"level":3}',
                "external_id": "t",
                "children": "tc",
            }
        )
        cmap["tc"] = Array([])
    md = server._doc_to_markdown(root["document"])
    assert "> [!WARNING]\n> Heads up" in md
    assert "$$\na^2+b^2\n$$" in md
    assert "### Details" in md


def test_page_mention_roundtrip():
    # forward: [label](mention:<id>) -> a "$" op carrying the page-mention attribute
    delta = server._md_inline_to_delta("See [Home](mention:pg123) now")
    mention = next(o for o in delta if o.get("attributes", {}).get("mention"))
    assert mention["insert"] == "$"
    assert mention["attributes"]["mention"] == {"type": "page", "page_id": "pg123"}
    # reverse: the mention op renders back to the same Markdown link
    rendered = server._inline_md(
        [("$", {"mention": {"type": "page", "page_id": "pg123"}})]
    )
    assert rendered == "[](mention:pg123)"
