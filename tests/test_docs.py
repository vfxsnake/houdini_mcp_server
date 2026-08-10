"""Checks for the documentation index, search and doc resources/tools.

Needs a Houdini install for its shipped help, but no running Houdini and no
bridge. Builds a throwaway index in a temp file rather than touching the cached
one, so a run here never disturbs the real index.

    python tests/test_docs.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402

from server.config import Config  # noqa: E402
from server.docs import DocsIndex, _to_fts_query, find_help_dir  # noqa: E402
from server.main import build_server  # noqa: E402


async def run_checks() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}  {detail}")

    print("\n== Help discovery ==")
    help_dir = find_help_dir()
    if help_dir is None:
        print("  SKIP  no Houdini help directory found; set HOUDINI_HELP_DIR")
        return 0
    print(f"  help dir: {help_dir}")
    check("help dir contains hom.zip", (help_dir / "hom.zip").is_file())

    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "docs.sqlite"
        docs = DocsIndex(index_path=index_path, help_dir=help_dir)

        print("\n== Index build ==")
        total = docs.build(force=True)
        print(f"  indexed {total} pages")
        check("indexed a plausible number of pages", total > 5000, str(total))
        counts = docs.namespaces()
        print(f"  namespaces: {counts}")
        for namespace in ("hom", "vex", "apex", "nodes"):
            check(f"{namespace} namespace populated",
                  counts.get(namespace, 0) > 100, str(counts.get(namespace)))

        print("\n== Query parsing ==")
        check("terms are OR-ed, not AND-ed",
              " OR " in _to_fts_query("point attribute"), _to_fts_query("point attribute"))
        check("dotted identifiers survive",
              '"hou.Node"' in _to_fts_query("hou.Node"), _to_fts_query("hou.Node"))
        check("stopwords dropped",
              "how" not in _to_fts_query("how do I scatter points"),
              _to_fts_query("how do I scatter points"))
        check("explicit FTS syntax passed through",
              _to_fts_query('"exact phrase"') == '"exact phrase"')
        try:
            _to_fts_query("   ")
            check("empty query rejected", False)
        except ValueError:
            check("empty query rejected", True)

        print("\n== Search quality ==")
        hom_hit = docs.search("hou.Node", namespace="hom", limit=5)
        check("exact identifier ranks first",
              hom_hit[0]["topic"] == "hou/Node", hom_hit[0]["topic"])

        vex_hit = docs.search("how do I read a point attribute", namespace="vex", limit=5)
        topics = [h["topic"] for h in vex_hit]
        check("natural question finds attribute functions",
              any("attrib" in t or "point" in t for t in topics), str(topics[:4]))

        node_hit = docs.search("scatter points on a surface", namespace="nodes", limit=5)
        check("node search finds the Scatter SOP",
              any(h["topic"].startswith("sop/scatter") for h in node_hit),
              str([h["topic"] for h in node_hit][:4]))

        check("namespace filter is honoured",
              all(h["namespace"] == "vex"
                  for h in docs.search("length", namespace="vex", limit=5)))

        # SideFX suffixes superseded node pages with '-'. sop/scatter- is the old
        # doc and is shorter, so bm25 alone puts it above the current 2.0 page --
        # which would have Claude quoting parameters that no longer exist.
        scatter = [h["topic"] for h in docs.search("scatter", namespace="nodes", limit=6)]
        check("current page outranks its superseded twin",
              "sop/scatter" in scatter
              and scatter.index("sop/scatter") < scatter.index("sop/scatter-")
              if "sop/scatter-" in scatter else "sop/scatter" in scatter,
              str(scatter[:4]))
        check("legacy pages are flagged",
              all(h["legacy"] == (1 if h["topic"].endswith("-") else 0)
                  for h in docs.search("scatter", namespace="nodes", limit=6)))
        check("results carry a summary",
              bool(docs.search("scatter", namespace="nodes", limit=1)[0]["summary"]))

        print("\n== Page retrieval ==")
        page = docs.get("hom", "hou/Node")
        check("exact page fetched", page is not None and page["title"] == "hou.Node")
        check("page body is the real help text",
              page is not None and "#type: homclass" in page["body"])
        check("missing page returns None", docs.get("hom", "hou/NoSuchThing") is None)
        check("loose name resolves",
              any(c["topic"] == "hou/Node" for c in docs.resolve("hom", "Node")))

        print("\n== Stale index ==")
        import sqlite3
        docs.close()
        # Close explicitly: sqlite3's context manager commits but does not close,
        # and Windows will not let the rebuild replace a file still held open.
        conn = sqlite3.connect(index_path)
        try:
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()
        check("stale schema is detected", docs._is_stale())
        check("stale index rebuilds itself on next use", docs.count() > 5000)
        check("rebuild restores the current schema version", not docs._is_stale())

        print("\n== Through MCP ==")
        mcp, houdini = build_server(Config(bridge_port=1), docs=docs)
        try:
            async with Client(mcp) as client:
                templates = {str(t.uriTemplate) for t in await client.list_resource_templates()}
                tool_names = {t.name for t in await client.list_tools()}
                for namespace in ("hom", "vex", "apex", "nodes"):
                    check(f"docs://{namespace} template registered",
                          f"docs://{namespace}/{{topic*}}" in templates, str(templates))
                check("search_docs registered", "search_docs" in tool_names)
                check("get_doc registered", "get_doc" in tool_names)

                result = json.loads(
                    (await client.call_tool(
                        "search_docs", {"query": "scatter points", "namespace": "nodes",
                                        "limit": 3})).content[0].text
                )
                check("search_docs returns results", result["count"] > 0)
                check("search_docs returns usable URIs",
                      result["results"][0]["uri"].startswith("docs://nodes/"),
                      result["results"][0]["uri"])

                doc = (await client.call_tool(
                    "get_doc", {"namespace": "hom", "topic": "hou/Node"})).content[0].text
                check("get_doc returns the page", "hou.Node" in doc)
                check("get_doc adds a source line", "source: docs://hom/hou/Node" in doc)

                resource = (await client.read_resource("docs://vex/functions/length"))[0].text
                check("docs resource reads a page", "magnitude of a vector" in resource)

                # Search results must not be dead ends: whatever search returns has
                # to be readable back through get_doc.
                hit = result["results"][0]
                round_trip = (await client.call_tool(
                    "get_doc", {"namespace": hit["namespace"],
                                "topic": hit["topic"]})).content[0].text
                check("search hits are readable via get_doc", len(round_trip) > 200)

                try:
                    await client.call_tool("search_docs", {"query": "x", "namespace": "bogus"})
                    check("bad namespace rejected", False)
                except Exception as exc:  # noqa: BLE001
                    check("bad namespace rejected", "Unknown namespace" in str(exc),
                          str(exc)[:120])

                try:
                    await client.call_tool(
                        "get_doc", {"namespace": "hom", "topic": "definitely/not/here"})
                    check("missing doc raises", False)
                except Exception as exc:  # noqa: BLE001
                    check("missing doc suggests search_docs",
                          "search_docs" in str(exc) or "Did you mean" in str(exc),
                          str(exc)[:120])
        finally:
            await houdini.aclose()
            docs.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("All docs checks passed.")
    return 0


def test_docs() -> None:
    assert asyncio.run(run_checks()) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_checks()))
