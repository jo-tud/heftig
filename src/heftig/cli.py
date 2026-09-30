"""Command line interface: ``heftig <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import threading
from pathlib import Path

from . import __version__


def _archive():
    from .archive import Archive
    from .config import get_settings

    return Archive(get_settings())


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args) -> int:
    from . import auth

    a = _archive()
    print(f"Archive: {a.paths.root}")
    if auth.user_count(a.conn) > 0:
        print("A user already exists. To change the password: heftig set-password <name>")
        return 0
    username = args.username or input("Username: ").strip()
    password = _read_password(args.password_stdin)
    try:
        auth.create_user(a.conn, username, password)
    except auth.AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"User “{username}” created. Start with: heftig serve (web) and heftig worker.")
    return 0


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    while True:
        p1 = getpass.getpass("Password (at least 10 characters): ")
        p2 = getpass.getpass("Repeat password: ")
        if p1 == p2:
            return p1
        print("Passwords do not match.", file=sys.stderr)


def cmd_set_password(args) -> int:
    from . import auth

    a = _archive()
    try:
        auth.set_password(a.conn, args.username, _read_password(args.password_stdin))
    except auth.AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("Password changed, existing sessions signed out.")
    return 0


def cmd_token(args) -> int:
    from . import auth

    a = _archive()
    if args.action == "list":
        _print(auth.list_tokens(a.conn))
    elif args.action == "create":
        row = a.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            print("Run `heftig init` first.", file=sys.stderr)
            return 1
        scope = "read" if args.read_only else "full"
        tid, token = auth.create_api_token(a.conn, row[0], args.name or "CLI", scope)
        print(f"Token #{tid} (shown only now):\n{token}")
    elif args.action == "revoke":
        if not auth.revoke_token(a.conn, int(args.name)):
            print("Token not found.", file=sys.stderr)
            return 1
        print("Revoked.")
    return 0


def cmd_recheck_dates(args) -> int:
    from .processing import revalidate_dates

    r = revalidate_dates(_archive())
    print(
        f"{r['checked']} date suggestions checked, {r['applied']} now found in the text and accepted; "
        f"{r['as_of']} document(s) without a date got their as-of date."
    )
    return 0


def cmd_blank_pages(args) -> int:
    from .maintenance import detect_blank_pages

    r = detect_blank_pages(_archive())
    print(f"{r['pages']} blank page(s) found in {r['documents']} document(s).")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    try:
        return run(args.url, args.token_file, args.public_url)
    except ModuleNotFoundError as e:
        print(f"heftig mcp: {e}. Install with: pip install 'heftig[mcp]'", file=sys.stderr)
        return 2


def cmd_serve(args) -> int:
    import uvicorn

    from .config import get_settings
    from .web.app import create_app

    s = get_settings()
    uvicorn.run(
        create_app(s),
        log_config=_uvicorn_log_config(),
        host=args.host or s.host,
        port=args.port or s.port,
        proxy_headers=s.trust_proxy_headers,
        forwarded_allow_ips="*" if s.trust_proxy_headers else None,
        log_level=s.log_level.lower(),
        server_header=False,
    )
    return 0


class _NoQueryString(logging.Filter):
    """Access log lines without the query string: search words (?q=), typed suggestions and the
    Claude connection's questions stay out of the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def _uvicorn_log_config() -> dict:
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["no_query"] = {"()": _NoQueryString}
    cfg["handlers"]["access"]["filters"] = ["no_query"]
    return cfg


def _setup_logging() -> None:
    from .config import get_settings

    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_worker(args) -> int:
    import signal

    from .worker import Worker

    _setup_logging()
    w = Worker(_archive())
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    signal.signal(signal.SIGINT, lambda *_: w.stop.set())
    w.run_forever()
    return 0


def cmd_run(args) -> int:
    """Web server and worker in one process (simple local setup)."""
    from .worker import Worker

    _setup_logging()
    w = Worker(_archive())
    t = threading.Thread(target=w.run_forever, name="worker", daemon=True)
    t.start()
    try:
        return cmd_serve(args)
    finally:
        w.stop.set()
        t.join(timeout=30)


def cmd_ingest(args) -> int:
    from .ingest import ingest_path

    a = _archive()
    rc = 0
    for f in args.files:
        res = ingest_path(a, Path(f), args.source, paper=True if args.paper else None)
        _print(res.as_dict())
        if res.status == "rejected":
            rc = 2
    return rc


def cmd_ingest_eml(args) -> int:
    import hashlib

    from .imap_import import process_message

    a = _archive()
    for f in args.files:
        raw = Path(f).read_bytes()
        res = process_message(
            a, raw, account="eml-file", fallback_key=hashlib.sha256(raw).hexdigest()
        )
        _print({"file": f, "results": res})
    return 0


def cmd_imap_poll(args) -> int:
    from .imap_import import ImapPoller

    a = _archive()
    if not a.settings.imap_host:
        print("HEFTIG_IMAP_HOST is not set.", file=sys.stderr)
        return 1
    res = ImapPoller(a).poll()
    _print({k: v for k, v in res.items() if k != "results"} | {"parts": len(res["results"])})
    return 1 if res["error"] else 0


def cmd_process(args) -> int:
    from .worker import run_until_idle

    _setup_logging()
    n = run_until_idle(_archive())
    print(f"{n} job(s) processed.")
    return 0


def cmd_reprocess(args) -> int:
    from .processing import reprocess

    a = _archive()
    ids = args.ids
    if args.all:
        ids = [r[0] for r in a.conn.execute("SELECT id FROM documents ORDER BY ingest_sequence")]
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    jobs = reprocess(a, ids, stages)
    print(f"{len(jobs)} job(s) queued ({', '.join(stages)}).")
    return 0


def cmd_check(args) -> int:
    from .maintenance import check

    rep = check(_archive(), verify_hashes=not args.quick)
    _print(rep)
    return 0 if rep["ok"] else 3


def cmd_repair(args) -> int:
    from .maintenance import repair

    rep = repair(_archive(), adopt_orphans=args.adopt_orphans)
    _print(rep)
    return 0 if not rep["remaining_issues"] else 3


def cmd_reindex(args) -> int:
    from .maintenance import reindex

    print(f"Search index rebuilt: {reindex(_archive())} documents.")
    return 0


def cmd_rebuild_db(args) -> int:
    from .maintenance import rebuild_db

    rep = rebuild_db(_archive())
    _print(rep)
    return 0 if not rep["errors"] else 3


def cmd_export(args) -> int:
    from .maintenance import export_archive

    path = export_archive(_archive(), Path(args.dest), as_zip=args.zip)
    print(f"Export written: {path}")
    return 0


def cmd_import(args) -> int:
    from .maintenance import MaintenanceError, import_archive

    try:
        rep = import_archive(_archive(), Path(args.source))
    except MaintenanceError as e:
        print(f"Import aborted: {e}", file=sys.stderr)
        return 1
    _print(rep)
    return 0 if not rep["conflicts"] else 3


def cmd_backup(args) -> int:
    from .maintenance import backup

    print(f"Backup written: {backup(_archive(), Path(args.dest))}")
    return 0


def cmd_db_snapshot(args) -> int:
    from .maintenance import db_snapshot

    print(f"Snapshot written: {db_snapshot(_archive())}")
    return 0


def cmd_restore(args) -> int:
    from .maintenance import MaintenanceError, restore

    try:
        restore(Path(args.backup), Path(args.target))
    except MaintenanceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Restored to {args.target}. Next: HEFTIG_ARCHIVE_DIR={args.target} heftig check")
    return 0


def cmd_duplicates(args) -> int:
    from .duplicates import open_pairs, scan_all

    a = _archive()
    if args.scan:
        print(f"{scan_all(a)} possible duplicate(s) open.")
    if args.resolve_identical:
        from .duplicates import resolve_identical

        n = len(resolve_identical(a))
        print(f"{n} identical duplicate(s) moved to the trash.")
    for p in open_pairs(a.conn):
        print(
            f"{p['score']:.2f}  {p['a']['title']}  <->  {p['b']['title']}  ({', '.join(p['reasons'])})"
        )
        print(f"      /duplicates/{p['a']['id']}/{p['b']['id']}")
    return 0


def cmd_schema(args) -> int:
    from .models import DocumentMetadata

    _print(DocumentMetadata.model_json_schema())
    return 0


def cmd_status(args) -> int:
    from .maintenance import status
    from .providers.registry import describe

    a = _archive()
    _print({**status(a), "providers": describe(a.settings), "archive": str(a.paths.root)})
    return 0


def cmd_search(args) -> int:
    from .search import SearchParams, search

    res = search(_archive().conn, SearchParams(q=" ".join(args.query), per_page=args.limit))
    d = res.as_dict()
    for it in d["items"]:
        it.pop("snippet_html", None)
    _print(d)
    return 0


def cmd_embed(args) -> int:
    from . import semantic

    a = _archive()
    if not semantic.available(a.settings):
        print("The search by meaning is off (Settings -> Search, or HEFTIG_SEMANTIC_SEARCH=true).",
              file=sys.stderr)  # fmt: skip
        return 2
    r = semantic.catch_up(a)
    _print({**r, **semantic.status(a.conn, a.settings)})
    return 1 if r.get("error") else 0


def cmd_search_eval(args) -> int:
    from . import searcheval

    a = _archive()
    cases, problems = searcheval.load_cases(a.conn, Path(args.file))
    for msg in problems:
        print(f"warning: {msg}", file=sys.stderr)
    if not cases:
        print("No queries with known documents.", file=sys.stderr)
        return 2
    summary, results = searcheval.evaluate(a.conn, cases)
    if args.json:
        print(searcheval.as_json(summary, results))
    else:
        print(searcheval.report(summary, results, searcheval.titles(a.conn)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="heftig", description="Heftig – local document archive")
    p.add_argument("--version", action="version", version=f"heftig {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Create the archive and its single user")
    s.add_argument("--username")
    s.add_argument("--password-stdin", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("set-password", help="Change the password")
    s.add_argument("username")
    s.add_argument("--password-stdin", action="store_true")
    s.set_defaults(fn=cmd_set_password)

    s = sub.add_parser("token", help="Manage API tokens")
    s.add_argument("action", choices=["list", "create", "revoke"])
    s.add_argument("name", nargs="?", help="name (create) or ID (revoke)")
    s.add_argument("--read-only", action="store_true", help="read-only token (e.g. for MCP)")
    s.set_defaults(fn=cmd_token)

    for name, fn, help_ in (
        ("serve", cmd_serve, "Start the web server"),
        ("run", cmd_run, "Web server and worker in one process"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--host")
        s.add_argument("--port", type=int)
        s.set_defaults(fn=fn)

    sub.add_parser("worker", help="Start the background worker").set_defaults(fn=cmd_worker)
    sub.add_parser(
        "recheck-dates",
        help="Re-check suggested document dates with the current rules (no AI)",
    ).set_defaults(fn=cmd_recheck_dates)
    sub.add_parser(
        "blank-pages", help="Find blank pages (e.g. empty backs of duplex scans) in all documents"
    ).set_defaults(fn=cmd_blank_pages)

    s = sub.add_parser(
        "mcp", help="MCP server (stdio) for Claude: search and read the archive, read-only"
    )
    s.add_argument(
        "--url", help="Heftig address (default: $HEFTIG_MCP_URL or http://127.0.0.1:8765)"
    )
    s.add_argument("--token-file", help="file with the API token (otherwise $HEFTIG_MCP_TOKEN)")
    s.add_argument("--public-url", help="address for links in answers (default: --url)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("ingest", help="Add files directly")
    s.add_argument("files", nargs="+")
    s.add_argument("--source", default="folder", choices=["folder", "scanner", "api", "web"])
    s.add_argument("--paper", action="store_true", help="mark as paper document")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("ingest-eml", help="Import e-mail files (.eml) as if from the mailbox")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_ingest_eml)

    sub.add_parser("imap-poll", help="Check the IMAP mailbox once").set_defaults(fn=cmd_imap_poll)
    sub.add_parser("process", help="Process queued jobs now").set_defaults(fn=cmd_process)

    s = sub.add_parser("reprocess", help="Reprocess documents")
    s.add_argument("ids", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--stages", default="extract,classify", help="extract,classify")
    s.set_defaults(fn=cmd_reprocess)

    s = sub.add_parser("check", help="Integrity check (hashes, sidecars, index)")
    s.add_argument("--quick", action="store_true", help="do not recompute hashes")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("repair", help="Safely complete interrupted operations")
    s.add_argument("--adopt-orphans", action="store_true")
    s.set_defaults(fn=cmd_repair)

    sub.add_parser("reindex", help="Rebuild the search index").set_defaults(fn=cmd_reindex)
    sub.add_parser("rebuild-db", help="Rebuild document data from sidecars").set_defaults(
        fn=cmd_rebuild_db
    )

    s = sub.add_parser("export", help="Write a portable export")
    s.add_argument("dest")
    s.add_argument("--zip", action="store_true")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("import", help="Import an export (idempotent)")
    s.add_argument("source")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("backup", help="Write a consistent full backup")
    s.add_argument("dest")
    s.set_defaults(fn=cmd_backup)

    sub.add_parser("db-snapshot", help="Consistent database copy for backup tools").set_defaults(
        fn=cmd_db_snapshot
    )

    s = sub.add_parser("restore", help="Restore a backup into an empty directory")
    s.add_argument("backup")
    s.add_argument("target")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("duplicates", help="Show possible duplicates by content")
    s.add_argument("--scan", action="store_true", help="check the whole archive first")
    s.add_argument("--resolve-identical", action="store_true",
                   help="resolve truly identical pairs (second copy to the trash)")  # fmt: skip
    s.set_defaults(fn=cmd_duplicates)

    sub.add_parser("schema", help="Print the JSON schema of metadata.json").set_defaults(
        fn=cmd_schema
    )
    sub.add_parser("status", help="Show status").set_defaults(fn=cmd_status)

    s = sub.add_parser("search", help="Search from the command line")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    sub.add_parser(
        "embed", help="Search by meaning: download the model if needed, embed new documents now"
    ).set_defaults(fn=cmd_embed)

    s = sub.add_parser(
        "search-eval", help="Measure search quality with queries and the documents they should find"
    )
    s.add_argument("file", help='JSON: [{"q": "...", "expect": ["<title or ID>"], "also": [...]}]')
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    # everything Heftig writes (archive, exports, backups, snapshots) is for the owner only
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
