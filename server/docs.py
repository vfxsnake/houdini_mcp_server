"""Local SideFX documentation: discovery, indexing, search and retrieval.

Houdini ships its complete help as plain-text wiki markup in zip archives under
`$HFS/houdini/help`, so nothing here scrapes sidefx.com. Pages are read from the
archives on demand; only a small FTS index is materialised on disk.
"""

from __future__ import annotations

import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# namespace -> (archive, path prefix within it). A prefix of "" takes everything.
# `apex` spans two archives because SideFX ships no apex.zip: the operator pages
# live under nodes.zip, the concept pages under character.zip.
NAMESPACES: dict[str, list[tuple[str, str]]] = {
    "hom": [("hom.zip", "")],
    "vex": [("vex.zip", "")],
    "apex": [("nodes.zip", "apex/"), ("character.zip", "kinefx/")],
    "nodes": [("nodes.zip", "")],
}

_METADATA = re.compile(r"^#(\w[\w-]*):\s*(.*)$", re.M)
_TITLE = re.compile(r"^=\s*(.+?)\s*=\s*$", re.M)
_SUMMARY = re.compile(r'"""(.*?)"""', re.S)


def default_index_path() -> Path:
    override = os.environ.get("HOUDINI_DOCS_INDEX")
    if override:
        return Path(override)
    return Path.home() / ".houdini_mcp" / "docs_index.sqlite"


def find_help_dir() -> Path | None:
    """Locate $HFS/houdini/help, by env var or by looking where Houdini installs."""
    for var in ("HOUDINI_HELP_DIR", "HFS"):
        value = os.environ.get(var)
        if not value:
            continue
        candidate = Path(value)
        if var == "HFS":
            candidate = candidate / "houdini" / "help"
        if (candidate / "hom.zip").is_file():
            return candidate

    roots = [
        Path("C:/Program Files/Side Effects Software"),
        Path("/opt/hfs"),
        Path("/Applications/Houdini"),
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            candidate = entry / "houdini" / "help"
            if (candidate / "hom.zip").is_file():
                found.append(candidate)

    if not found:
        return None
    # Newest install wins, so a machine with several Houdinis indexes the current one.
    return sorted(found, key=lambda p: p.parent.parent.name)[-1]


@dataclass(frozen=True)
class Page:
    namespace: str
    topic: str          # archive-relative path without .txt, e.g. "hou/Node"
    name: str           # last path component, e.g. "Node"
    title: str
    summary: str
    kind: str           # from #type:
    context: str        # from #context:
    tags: str
    body: str

    @property
    def uri(self) -> str:
        return f"docs://{self.namespace}/{self.topic}"

    @property
    def legacy(self) -> int:
        """SideFX suffixes superseded node docs with '-'.

        `sop/scatter-` is the old page; `sop/scatter` is version 2.0. Both match a
        search for "scatter", and left alone the shorter old page often wins on
        bm25 -- which would have Claude quoting parameters that no longer exist.
        """
        return 1 if self.topic.endswith("-") else 0


def _parse(namespace: str, topic: str, text: str) -> Page:
    meta = {k.lower(): v.strip() for k, v in _METADATA.findall(text)}
    title_match = _TITLE.search(text)
    summary_match = _SUMMARY.search(text)

    name = topic.rsplit("/", 1)[-1]
    return Page(
        namespace=namespace,
        topic=topic,
        name=name,
        title=title_match.group(1) if title_match else name,
        summary=" ".join(summary_match.group(1).split()) if summary_match else "",
        kind=meta.get("type", ""),
        context=meta.get("context", ""),
        tags=meta.get("tags", ""),
        body=text,
    )


def iter_pages(help_dir: Path) -> Iterator[Page]:
    """Walk every archive once, yielding one Page per help topic."""
    seen: set[tuple[str, str]] = set()
    for namespace, sources in NAMESPACES.items():
        for archive_name, prefix in sources:
            archive = help_dir / archive_name
            if not archive.is_file():
                continue
            with zipfile.ZipFile(archive) as archive_file:
                for entry in archive_file.namelist():
                    if not entry.endswith(".txt") or not entry.startswith(prefix):
                        continue
                    topic = entry[:-4]
                    if (namespace, topic) in seen:
                        continue
                    seen.add((namespace, topic))
                    try:
                        text = archive_file.read(entry).decode("utf8", "replace")
                    except (KeyError, zipfile.BadZipFile):
                        continue
                    yield _parse(namespace, topic, text)


# Bump when the schema or the indexed fields change, so stale caches rebuild
# themselves instead of silently serving the old shape.
SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE pages USING fts5(
    namespace UNINDEXED,
    topic UNINDEXED,
    name,
    title,
    summary,
    tags,
    body,
    kind UNINDEXED,
    context UNINDEXED,
    legacy UNINDEXED,
    tokenize = "unicode61 tokenchars '_.'"
);
"""


class DocsIndex:
    """FTS5 index over the shipped help. Built once, then read-only."""

    def __init__(self, index_path: Path | None = None, help_dir: Path | None = None):
        self.index_path = index_path or default_index_path()
        self._help_dir = help_dir
        self._conn: sqlite3.Connection | None = None

    @property
    def help_dir(self) -> Path | None:
        if self._help_dir is None:
            self._help_dir = find_help_dir()
        return self._help_dir

    # -- building -----------------------------------------------------------

    def exists(self) -> bool:
        return self.index_path.is_file()

    def build(self, force: bool = False) -> int:
        """Index every page. Returns the number indexed."""
        if self.exists() and not force:
            return self.count()

        help_dir = self.help_dir
        if help_dir is None:
            raise FileNotFoundError(
                "Could not find Houdini's help directory. Set HOUDINI_HELP_DIR to "
                "the folder containing hom.zip (usually "
                "<install>/houdini/help), or set HFS to the Houdini install root."
            )

        self.close()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.index_path.with_suffix(".building")
        tmp_path.unlink(missing_ok=True)

        conn = sqlite3.connect(tmp_path)
        try:
            conn.executescript(SCHEMA)
            rows = (
                (p.namespace, p.topic, p.name, p.title, p.summary, p.tags,
                 p.body, p.kind, p.context, p.legacy)
                for p in iter_pages(help_dir)
            )
            conn.executemany(
                "INSERT INTO pages (namespace, topic, name, title, summary, tags,"
                " body, kind, context, legacy) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("help_dir", str(help_dir)), ("schema_version", SCHEMA_VERSION)],
            )
            conn.commit()
            total = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
        finally:
            conn.close()

        # Swap in atomically, so a crash mid-build never leaves a half index.
        tmp_path.replace(self.index_path)
        return total

    # -- reading ------------------------------------------------------------

    def _is_stale(self) -> bool:
        """An index from an older schema is worse than none -- rebuild it."""
        try:
            conn = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return True
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return row is None or row[0] != SCHEMA_VERSION
        except sqlite3.Error:
            return True
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.exists() or self._is_stale():
                self.build(force=True)
            self._conn = sqlite3.connect(
                f"file:{self.index_path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def count(self) -> int:
        return self._connect().execute("SELECT count(*) FROM pages").fetchone()[0]

    def namespaces(self) -> dict[str, int]:
        rows = self._connect().execute(
            "SELECT namespace, count(*) AS n FROM pages GROUP BY namespace"
        )
        return {row["namespace"]: row["n"] for row in rows}

    def search(
        self, query: str, namespace: str | None = None, limit: int = 10
    ) -> list[dict]:
        """Full-text search, exact name/title matches first.

        Ranking alone is not enough: "hou.Node" loses to shorter pages that merely
        mention it, because bm25 penalises the length of the real 54KB page. So an
        exact identifier match is promoted ahead of the ranked results.
        """
        conn = self._connect()
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def collect(rows) -> None:
            for row in rows:
                entry = dict(row)
                key = (entry["namespace"], entry["topic"])
                if key not in seen:
                    seen.add(key)
                    results.append(entry)

        columns = (
            "namespace, topic, name, title, summary, kind, context, legacy, "
            "substr(replace(summary, char(10), ' '), 1, 240) AS excerpt"
        )
        exact_sql = (
            f"SELECT {columns} FROM pages"
            " WHERE (name = :q COLLATE NOCASE OR title = :q COLLATE NOCASE)"
        )
        params: dict = {"q": query.strip(), "limit": limit}
        if namespace:
            exact_sql += " AND namespace = :ns"
            params["ns"] = namespace
        exact_sql += " ORDER BY legacy ASC LIMIT :limit"
        collect(conn.execute(exact_sql, params).fetchall())

        if len(results) >= limit:
            return results[:limit]

        match_sql = (
            "SELECT namespace, topic, name, title, summary, kind, context, legacy,"
            " snippet(pages, 6, '<<', '>>', ' … ', 18) AS excerpt"
            " FROM pages WHERE pages MATCH :match"
        )
        params["match"] = _to_fts_query(query)
        if namespace:
            match_sql += " AND namespace = :ns"
        # Current pages before superseded ones, then weight the short identifying
        # fields above the body text.
        match_sql += (
            " ORDER BY legacy ASC,"
            " bm25(pages, 0, 0, 10.0, 8.0, 4.0, 2.0, 1.0) LIMIT :limit"
        )

        try:
            collect(conn.execute(match_sql, params).fetchall())
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Could not parse search query {query!r}: {exc}") from exc
        return results[:limit]

    def get(self, namespace: str, topic: str) -> dict | None:
        """Exact topic lookup, e.g. ('hom', 'hou/Node')."""
        row = self._connect().execute(
            "SELECT namespace, topic, name, title, summary, kind, context, body"
            " FROM pages WHERE namespace = ? AND topic = ?",
            (namespace, topic),
        ).fetchone()
        return dict(row) if row else None

    def resolve(self, namespace: str, topic: str) -> list[dict]:
        """Candidates for a loose topic, so 'Node' finds 'hou/Node'."""
        name = topic.rsplit("/", 1)[-1]
        rows = self._connect().execute(
            "SELECT namespace, topic, name, title, summary FROM pages"
            " WHERE namespace = ? AND (name = ? COLLATE NOCASE"
            "       OR topic LIKE ? COLLATE NOCASE) LIMIT 25",
            (namespace, name, f"%{topic}"),
        ).fetchall()
        return [dict(row) for row in rows]


# Words that carry no signal in a "how do I ..." question but, under FTS5's
# implicit AND, would exclude every page that happens not to contain them.
_STOPWORDS = frozenset(
    """a an and are as at be by can do does for from get from how i if in into is it
    me my of on or that the to use using was what when where which who why will with
    you your""".split()
)


def _to_fts_query(query: str) -> str:
    """Make everyday queries safe for FTS5, which treats much punctuation as syntax.

    Terms are OR-ed rather than AND-ed. FTS5 defaults to AND, which makes a natural
    question like "how do I read a point attribute" match almost nothing; bm25 still
    ranks pages matching more terms first, so recall improves without losing order.
    """
    query = query.strip()
    if not query:
        raise ValueError("Search query is empty")
    # Respect deliberate FTS syntax rather than mangling it.
    if any(op in query for op in ('"', " OR ", " AND ", " NOT ", "NEAR(")):
        return query

    terms = [t for t in re.split(r"[^\w.*_]+", query) if t]
    meaningful = [t for t in terms if t.lower() not in _STOPWORDS]
    terms = meaningful or terms
    if not terms:
        raise ValueError(f"No searchable terms in {query!r}")
    return " OR ".join(t if t.endswith("*") else f'"{t}"' for t in terms)


def format_page(page: dict) -> str:
    """Render a page for reading: identity first, then the help text as shipped."""
    header = [f"# {page['title']}", ""]
    facts = []
    if page.get("kind"):
        facts.append(f"type: {page['kind']}")
    if page.get("context"):
        facts.append(f"context: {page['context']}")
    facts.append(f"source: docs://{page['namespace']}/{page['topic']}")
    header.append(" · ".join(facts))
    if page.get("summary"):
        header += ["", page["summary"]]
    header += ["", "---", ""]
    return "\n".join(header) + page.get("body", "")
