"""End-to-end smoke test against real processes (web server + worker + Tesseract).

Covers the "Definition of Done": a scan PDF via the consume folder, a phone photo via the
browser API, a digital PDF via e-mail (.eml, same code path as IMAP), real offline OCR, search,
byte-identical originals, worker crash + restart, locked fields across reprocessing,
export -> import into an empty archive, index rebuild and backup -> restore.

    make smoke          # needs tesseract + German language data, and the dev dependencies

The test documents and search queries are German on purpose (German OCR, date and search rules).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.helpers import image_bytes, scan_pdf, text_image, text_pdf  # noqa: E402

PASSWORD = "smoke-test-password"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  OK   " if ok else "  FAIL ") + name + (f" – {detail}" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Instance:
    def __init__(self, base: Path, name: str):
        self.dir = base / name
        self.archive = self.dir / "archive"
        self.consume = self.dir / "consume"
        self.consume.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.env = dict(
            os.environ,
            HEFTIG_ARCHIVE_DIR=str(self.archive),
            HEFTIG_CONSUME_DIR=str(self.consume),
            HEFTIG_PORT=str(self.port),
            HEFTIG_CONSUME_POLL_SECONDS="1",
            HEFTIG_CONSUME_MIN_AGE_SECONDS="0",
            HEFTIG_OCR_PROVIDER="tesseract",
            HEFTIG_CLASSIFY_PROVIDER="rules",
            HEFTIG_WORKER_CONCURRENCY="2",
            HEFTIG_LOG_LEVEL="WARNING",
        )
        self.procs: dict[str, subprocess.Popen] = {}
        self.url = f"http://127.0.0.1:{self.port}"

    def cli(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "heftig.cli", *args],
            env=self.env, input=stdin, capture_output=True, text=True, check=False,
        )  # fmt: skip

    def start(self, what: str) -> None:
        log = open(self.dir / f"{what}.log", "a")  # noqa: SIM115
        self.procs[what] = subprocess.Popen(
            [sys.executable, "-m", "heftig.cli", what], env=self.env, stdout=log, stderr=log
        )

    def kill(self, what: str, sig=signal.SIGTERM) -> None:
        p = self.procs.pop(what)
        p.send_signal(sig)
        p.wait(timeout=60)

    def stop_all(self) -> None:
        for w in list(self.procs):
            self.kill(w)

    def wait_ready(self) -> None:
        for _ in range(100):
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("web server did not start")


def login(inst: Instance) -> tuple[httpx.Client, str]:
    c = httpx.Client(base_url=inst.url, timeout=60)
    r = c.post("/api/auth/login", json={"username": "smoke", "password": PASSWORD})
    r.raise_for_status()
    return c, r.json()["csrf_token"]


def wait_idle(c: httpx.Client, expect_docs: int, timeout: float = 300) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        counts = c.get("/api/jobs").json()["counts"]
        total = c.get("/api/documents", params={"per_page": 1}).json()["total"]
        if total >= expect_docs and counts["queued"] == 0 and counts["processing"] == 0:
            return counts
        time.sleep(1)
    raise RuntimeError(f"timeout waiting for jobs (docs={total}, counts={counts})")


def ids(c: httpx.Client, **params) -> list[str]:
    return [
        i["id"] for i in c.get("/api/documents", params={"per_page": 100, **params}).json()["items"]
    ]


def main() -> int:
    if not shutil.which("tesseract"):
        print("tesseract is missing – the smoke test needs offline OCR")
        return 2
    base = ROOT / ".smoke" / time.strftime("%Y%m%d-%H%M%S")
    base.mkdir(parents=True)
    print(f"Smoke test in {base}")
    a = Instance(base, "a")
    try:
        r = a.cli("init", "--username", "smoke", "--password-stdin", stdin=PASSWORD + "\n")
        check("heftig init creates the user", r.returncode == 0, r.stderr.strip())
        a.start("serve")
        a.start("worker")
        a.wait_ready()
        c, csrf = login(a)
        h = {"X-CSRF-Token": csrf}

        # 1. paper via scanner folder (image-only PDF -> real OCR)
        scan = scan_pdf([
            "Stadtwerke Beispielstadt GmbH\nJahresabrechnung Strom\nDatum: 15.01.2026\n"
            "Vertragsnummer: 55512345\nGesamtbetrag: 812,40 EUR",
            "Seite 2\nHinweise zur Abrechnung und zum Zählerstand",
        ])  # fmt: skip
        (a.consume / "scan_0001.pdf.part").write_bytes(scan)  # scanner still writing
        time.sleep(2.5)
        os.rename(a.consume / "scan_0001.pdf.part", a.consume / "scan_0001.pdf")
        # 2. phone photo via browser upload
        photo = image_bytes(
            text_image("Kassenbon\nBäckerei Sonnenschein\nSumme 12,40 EUR\n14.09.2026"), "JPEG"
        )
        up = c.post("/api/documents", files=[("files", ("IMG_2031.jpg", photo, "image/jpeg"))],
                    data={"kind": "paper"}, headers=h).json()["results"][0]  # fmt: skip
        check("phone photo added via browser API", up["status"] == "created")
        # 3. digital PDF via e-mail
        digital = text_pdf(["Telekom Deutschland GmbH\nIhre Rechnung September 2026\n"
                            "Rechnungsdatum: 03.09.2026\nRechnungsbetrag: 39,95 EUR"])  # fmt: skip
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = (
            "rechnung@example.org",
            "archiv@example.org",
            "Fwd: Rechnung",
        )
        m["Message-ID"] = "<smoke-1@example.org>"
        m.set_content("Weitergeleitet.")
        m.add_attachment(digital, maintype="application", subtype="pdf", filename="rechnung.pdf")
        eml = base / "mail.eml"
        eml.write_bytes(m.as_bytes())
        r = a.cli("ingest-eml", str(eml))
        check("e-mail (.eml, IMAP path) added", r.returncode == 0 and '"created"' in r.stdout)
        a.cli("ingest-eml", str(eml))  # idempotent

        wait_idle(c, 3)
        check(
            "scan picked up from the consume folder, source removed", not any(a.consume.iterdir())
        )
        docs = c.get("/api/documents", params={"per_page": 50}).json()["items"]
        check("exactly 3 documents (repeating the mail creates no duplicate)", len(docs) == 3,
              str(len(docs)))  # fmt: skip
        by_src = {d["source"]: d for d in docs}
        for src, data in (("scanner", scan), ("web", photo), ("email", digital)):
            d = by_src.get(src)
            orig = c.get(f"/api/documents/{d['id']}/original").content if d else b""
            check(
                f"original byte-identical ({src})",
                hashlib.sha256(orig).digest() == hashlib.sha256(data).digest(),
            )
        for q, src in (("Vertragsnummer 55512345", "scanner"), ("Bäckerei Sonnenschein", "web"),
                       ("Telekomm Rechnung", "email"), ("Zählerstand", "scanner")):  # fmt: skip
            found = ids(c, q=q)
            check(
                f"search “{q}” finds the {src} document",
                bool(found) and found[0] == by_src[src]["id"],
                str(found[:2]),
            )
        scan_meta = c.get(f"/api/documents/{by_src['scanner']['id']}").json()
        check("scan: 2 pages via OCR, date recognized",
              [p["method"] for p in scan_meta["pages"]] == ["ocr", "ocr"]
              and scan_meta["metadata"]["document_date"] == "2026-01-15",
              f"{scan_meta['metadata']['document_date']}")  # fmt: skip
        check("scan marked as paper, not yet filed",
              scan_meta["metadata"]["paper"] and scan_meta["filing_position"] is None)  # fmt: skip

        # filing, locks, reprocessing
        sid = by_src["scanner"]["id"]
        c.post(f"/api/documents/{sid}/filing", json={"action": "file"}, headers=h)
        c.post(f"/api/documents/{by_src['web']['id']}/filing", json={"action": "file"}, headers=h)
        c.patch(f"/api/documents/{sid}", json={"title": "Stromabrechnung 2025 (manuell)",
                                                "correspondent": "Stadtwerke Beispielstadt"}, headers=h)  # fmt: skip
        c.post(
            f"/api/documents/{sid}/reprocess", json={"stages": ["extract", "classify"]}, headers=h
        )
        wait_idle(c, 3)
        md = c.get(f"/api/documents/{sid}").json()
        check("corrections survive reprocessing",
              md["metadata"]["title"] == "Stromabrechnung 2025 (manuell)"
              and md["metadata"]["correspondent"] == "Stadtwerke Beispielstadt")  # fmt: skip
        check("filing position visible", md["filing_position"]["position_from_top"] == 2)

        # worker crash + restart while jobs are running
        for i in range(4):
            (a.consume / f"stapel_{i}.pdf").write_bytes(
                scan_pdf(
                    [f"Stapel Dokument {i}\nVersicherung Beispiel AG\nDatum: 0{i + 1}.02.2026"] * 2
                )
            )
        t0 = time.time()
        while time.time() - t0 < 60:
            counts = c.get("/api/jobs").json()["counts"]
            if counts["processing"] > 0:
                break
            time.sleep(0.2)
        a.kill("worker", signal.SIGKILL)
        check("worker killed during processing", counts["processing"] > 0, str(counts))
        a.start("worker")
        wait_idle(c, 7)
        final = c.get("/api/documents", params={"per_page": 50}).json()
        statuses = {i["status"] for i in final["items"]}
        check("all jobs done after restart", final["total"] == 7 and statuses <= {"done", "needs_review"},
              str(statuses))  # fmt: skip
        r = a.cli("check")
        check(
            "integrity check without findings",
            r.returncode == 0,
            r.stdout[-300:] if r.returncode else "",
        )

        before = {
            q: ids(c, q=q) for q in ("Telekomm Rechnung", "55512345", "Stapel Versicherung", "")
        }
        r = a.cli("reindex")
        check("index rebuild keeps search results", {q: ids(c, q=q) for q in before} == before)

        # export -> import into an empty instance
        a.stop_all()
        exp = a.cli("export", str(base / "exports"))
        exp_path = exp.stdout.strip().split(": ", 1)[-1]
        check("export written", exp.returncode == 0, exp_path)
        b = Instance(base, "b")
        b.cli("init", "--username", "smoke", "--password-stdin", stdin=PASSWORD + "\n")
        imp = b.cli("import", exp_path)
        rep = json.loads(imp.stdout)
        check(
            "import into an empty instance without conflicts",
            imp.returncode == 0 and rep["imported"] == 7,
            str(rep)[:200],
        )
        b.start("serve")
        b.wait_ready()
        cb, _ = login(b)
        check("search results identical after import", {q: ids(cb, q=q) for q in before} == before)
        sa = json.loads(a.cli("status").stdout)
        order_a = [
            json.loads(ln)["ingest_sequence"]
            for ln in (Path(exp_path) / "metadata.jsonl").read_text().splitlines()
        ]
        rows_b = sorted(
            (d["ingest_sequence"], d["id"])
            for d in cb.get("/api/documents", params={"per_page": 50}).json()["items"]
        )
        check("ingest order preserved", [r[0] for r in rows_b] == sorted(order_a))
        fb = cb.get(f"/api/documents/{sid}").json()
        check("paper filing and locks preserved after import",
              fb["filing_position"]["position_from_top"] == 2 and fb["metadata"]["field_locks"].get("title"))  # fmt: skip
        b.stop_all()
        check("same number of documents", sa["documents"] == 7)

        # backup -> restore -> check
        bk = a.cli("backup", str(base / "backups"))
        bk_path = bk.stdout.strip().split(": ", 1)[-1]
        restored = base / "restored" / "archive"
        r = a.cli("restore", bk_path, str(restored))
        env_r = dict(a.env, HEFTIG_ARCHIVE_DIR=str(restored))
        chk = subprocess.run(
            [sys.executable, "-m", "heftig.cli", "check"],
            env=env_r,
            capture_output=True,
            text=True,
            check=False,
        )
        srch = subprocess.run([sys.executable, "-m", "heftig.cli", "search", "55512345"], env=env_r,
                              capture_output=True, text=True, check=False)  # fmt: skip
        check("backup → restore into a fresh directory, check ok",
              r.returncode == 0 and chk.returncode == 0 and sid in srch.stdout)  # fmt: skip
    finally:
        a.stop_all()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
