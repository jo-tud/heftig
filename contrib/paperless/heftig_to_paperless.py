#!/usr/bin/env python3
"""Upload a Heftig export into a Paperless-ngx instance.

Reads an *unzipped* Heftig export directory (``metadata.jsonl`` + ``originals/``) and uploads
every original via ``POST /api/documents/post_document/`` with title, created date,
correspondent, document type and tags. Correspondents, document types and tags are looked up by
name and created if missing. Documents whose MD5 checksum Paperless already knows are skipped,
so the script can be re-run after an interruption.

Field mapping and known losses: docs/paperless.md in the Heftig repository.

    PAPERLESS_URL=https://paperless.example.org PAPERLESS_TOKEN=... \\
        python contrib/paperless/heftig_to_paperless.py ./exp/heftig-export-20260928-120000 --dry-run

Requirements: Python 3.11+ and ``httpx`` (``pip install httpx``).

STATUS: this script has only been tested against a mocked Paperless-ngx API
(tests/test_paperless_script.py), not against a real instance. Try it with ``--dry-run`` and
then with ``--limit 3`` on a test instance before migrating everything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

TAXONOMY_ENDPOINTS = {
    "correspondent": "/api/correspondents/",
    "document_type": "/api/document_types/",
    "tag": "/api/tags/",
}
CHUNK = 1024 * 1024


class MigrationError(Exception):
    pass


@dataclass
class Plan:
    """What will be sent to Paperless for one Heftig document."""

    heftig_id: str
    path: str
    filename: str
    sha256: str
    md5: str
    title: str
    created: str | None
    correspondent: str | None
    document_type: str | None
    tags: list[str] = field(default_factory=list)
    # Heftig data without a Paperless field; printed for reference, not uploaded
    not_mapped: dict[str, Any] = field(default_factory=dict)


def _hash_file(path: Path) -> tuple[str, str]:
    # Paperless identifies originals by MD5 (not used for security here)
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def read_export(root: Path) -> list[dict[str, Any]]:
    """Metadata objects of an unzipped Heftig export, in ingest order."""
    jl = root / "metadata.jsonl"
    if not jl.is_file():
        raise MigrationError(f"{jl} not found - is this an (unzipped) Heftig export directory?")
    metas = []
    for n, line in enumerate(jl.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                metas.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise MigrationError(f"metadata.jsonl line {n}: {e.msg}") from e
    metas.sort(key=lambda m: m.get("ingest_sequence", 0))
    return metas


def plan_document(root: Path, meta: dict[str, Any]) -> Plan:
    """Map one Heftig metadata object onto Paperless upload fields and verify the original."""
    rel = meta["original_relpath"]
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise MigrationError(f"{meta['id']}: unsafe path {rel}")
    if not path.is_file():
        raise MigrationError(f"{meta['id']}: original {rel} missing")
    sha, md5 = _hash_file(path)
    if sha != meta["sha256"]:
        raise MigrationError(f"{meta['id']}: SHA-256 of {rel} does not match the metadata")
    filename = meta.get("original_filename") or path.name
    title = (meta.get("title") or "").strip() or Path(filename).stem
    not_mapped = {
        "heftig_id": meta["id"],
        "received_at": meta.get("received_at"),
        "ingest_sequence": meta.get("ingest_sequence"),
        "source": meta.get("source"),
    }
    if meta.get("filing_sequence") is not None:
        not_mapped["filing"] = {
            "filed_at": meta.get("filed_at"),
            "filing_section": meta.get("filing_section"),
            "filing_sequence": meta.get("filing_sequence"),
        }
    if meta.get("custom_fields"):
        not_mapped["custom_fields"] = meta["custom_fields"]
    if meta.get("field_locks"):
        not_mapped["field_locks"] = meta["field_locks"]
    return Plan(
        heftig_id=meta["id"],
        path=str(path),
        filename=filename,
        sha256=sha,
        md5=md5,
        title=title[:128],  # Paperless limits titles to 128 characters
        created=meta.get("document_date"),
        correspondent=meta.get("correspondent") or None,
        document_type=meta.get("document_type") or None,
        tags=list(dict.fromkeys(meta.get("tags") or [])),
        not_mapped=not_mapped,
    )


class PaperlessClient:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            timeout=120,
            transport=transport,
            follow_redirects=False,
        )
        self._ids: dict[tuple[str, str], int] = {}
        self.created_terms: list[tuple[str, str]] = []

    def close(self) -> None:
        self._http.close()

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise MigrationError(
                f"Paperless: {r.request.method} {r.request.url.path} -> HTTP {r.status_code} "
                f"{r.text[:300]}"
            )
        return r

    def exists(self, md5: str) -> bool:
        r = self._check(self._http.get("/api/documents/", params={"checksum__iexact": md5}))
        return int(r.json().get("count", 0)) > 0

    def term_id(self, kind: str, name: str) -> int:
        """ID of a correspondent / document type / tag, created if it does not exist."""
        key = (kind, name.casefold())
        if key in self._ids:
            return self._ids[key]
        endpoint = TAXONOMY_ENDPOINTS[kind]
        r = self._check(self._http.get(endpoint, params={"name__iexact": name}))
        results = r.json().get("results") or []
        if results:
            tid = int(results[0]["id"])
        else:
            # matching_algorithm 0 = "none": Paperless must not auto-assign the new term
            r = self._check(self._http.post(endpoint, json={"name": name, "matching_algorithm": 0}))
            tid = int(r.json()["id"])
            self.created_terms.append((kind, name))
        self._ids[key] = tid
        return tid

    def upload(self, plan: Plan) -> str:
        """Upload one original. Returns the Paperless consumption task id."""
        data: dict[str, Any] = {"title": plan.title}
        if plan.created:
            data["created"] = plan.created
        if plan.correspondent:
            data["correspondent"] = str(self.term_id("correspondent", plan.correspondent))
        if plan.document_type:
            data["document_type"] = str(self.term_id("document_type", plan.document_type))
        if plan.tags:
            data["tags"] = [str(self.term_id("tag", t)) for t in plan.tags]
        with open(plan.path, "rb") as f:
            r = self._check(
                self._http.post(
                    "/api/documents/post_document/",
                    data=data,
                    files={"document": (plan.filename, f, "application/octet-stream")},
                )
            )
        try:
            return str(r.json())
        except ValueError:
            return r.text.strip()


def migrate(
    root: Path, client: PaperlessClient | None, *, dry_run: bool, limit: int | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {"planned": 0, "uploaded": 0, "skipped_existing": 0, "errors": []}
    metas = read_export(root)
    if limit is not None:
        metas = metas[:limit]
    for meta in metas:
        try:
            plan = plan_document(root, meta)
        except (MigrationError, KeyError) as e:
            report["errors"].append(str(e))
            continue
        report["planned"] += 1
        if dry_run:
            print(json.dumps({"action": "upload", **asdict(plan)}, ensure_ascii=False))
            continue
        assert client is not None
        try:
            if client.exists(plan.md5):
                report["skipped_existing"] += 1
                print(f"skip  {plan.heftig_id}  already in Paperless ({plan.filename})")
                continue
            task = client.upload(plan)
        except (MigrationError, httpx.HTTPError) as e:
            report["errors"].append(f"{plan.heftig_id}: {e}")
            continue
        report["uploaded"] += 1
        print(f"sent  {plan.heftig_id}  task {task}  ({plan.filename})")
    if client is not None:
        report["created_terms"] = [f"{k}:{n}" for k, n in client.created_terms]
    return report


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    p = argparse.ArgumentParser(description="Upload a Heftig export to Paperless-ngx.")
    p.add_argument("export_dir", type=Path, help="unzipped heftig-export-* directory")
    p.add_argument("--dry-run", action="store_true", help="print planned uploads, send nothing")
    p.add_argument("--limit", type=int, help="only the first N documents (in ingest order)")
    args = p.parse_args(argv)

    client = None
    if not args.dry_run:
        url, token = os.environ.get("PAPERLESS_URL"), os.environ.get("PAPERLESS_TOKEN")
        if not url or not token:
            print("PAPERLESS_URL and PAPERLESS_TOKEN must be set.", file=sys.stderr)
            return 2
        client = PaperlessClient(url, token, transport=transport)
    try:
        report = migrate(args.export_dir, client, dry_run=args.dry_run, limit=args.limit)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
    if not args.dry_run and report["uploaded"]:
        print(
            "Paperless consumes uploads asynchronously - check its task list for failures.",
            file=sys.stderr,
        )
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
