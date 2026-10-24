"""Phase 5: review decisions on ingest files."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import db, ingest, review
from app.config import get_settings
from samples import make_epub, make_pdf
from test_catalog import _no, _noop, conn  # noqa: F401
from test_phase4 import drop  # noqa: F401


def _scan(conn, **kw):  # noqa: F811
    return ingest.scan(conn, get_settings(), _noop, _no, n_workers=0, **kw)


def _id(conn, path):  # noqa: F811
    return conn.execute("SELECT id FROM ingest_files WHERE path=?", (path,)).fetchone()[0]


def _f(conn, path):  # noqa: F811
    return conn.execute("SELECT * FROM ingest_files WHERE path=?", (path,)).fetchone()


@pytest.fixture()
def scanned(conn, drop):  # noqa: F811
    _scan(conn)
    return drop


def test_queue_holds_only_undecided_review_and_unmatched(conn, scanned):  # noqa: F811
    q = review.queue(conn)
    paths = [conn.execute("SELECT path FROM ingest_files WHERE id=?", (i,)).fetchone()[0] for i in q["ids"]]
    assert paths == ["the silmarillion.pdf", "Quarterly Tax Notes 2019.pdf"]   # review first, then unmatched
    assert q["counts"]["queue"] == 2 and q["counts"]["matched"] == 8
    st = ingest.stats(conn)
    assert st["review_queue"] == 2 and st["ready"] == 8 and st["decisions"] == {"pending": 15}


def test_detail_explains_candidates(conn, scanned):  # noqa: F811
    d = review.detail(conn, _id(conn, "the silmarillion.pdf"))
    assert d["selected"] == "OL1W" and d["reading"]["title"] == "the silmarillion"
    top = d["candidates"][0]
    assert top["ol_key"] == "OL1W" and top["authors"][0]["name"] == "J.R.R. Tolkien"
    assert top["title_sim"] == 100 and top["author_sim"] is None and "no author to compare" in top["why"]
    assert d["can_accept"] is True
    # ISBN evidence is called out
    d = review.detail(conn, _id(conn, "scans/random-scan-0042.pdf"))
    assert d["candidates"][0]["isbn_match"] and d["candidates"][0]["why"][0].startswith("ISBN 9780261102217")
    assert review.detail(conn, _id(conn, "locked.epub"))["can_accept"] is False
    with pytest.raises(KeyError):
        review.detail(conn, 99999)


def test_accept_choose_not_book_skip_and_undo(conn, scanned):  # noqa: F811
    sil, tax = _id(conn, "the silmarillion.pdf"), _id(conn, "Quarterly Tax Notes 2019.pdf")
    r = review.decide(conn, sil, "accept")
    assert r == {"id": sil, "next": tax, "remaining": 1}
    row = _f(conn, "the silmarillion.pdf")
    assert (row["decision"], row["decided_work_key"]) == ("accepted", "OL1W")
    # choose a different work and fix the details while accepting
    review.decide(conn, sil, "accept", work_key="OL27448W",
                  overrides={"title": "The Silmarillion", "authors": "Tolkien, J.R.R.", "series_index": "3",
                             "isbn": "0-261-10221-4"})
    row = _f(conn, "the silmarillion.pdf")
    assert row["decided_work_key"] == "OL27448W"
    rd = review.reading(row)
    assert rd["title"] == "The Silmarillion" and rd["authors"] == ["J.R.R. Tolkien"] and rd["isbns"] == ["9780261102217"]
    assert rd["source"]["title"] == "your edit"
    assert review.decide(conn, tax, "not_book")["remaining"] == 0
    assert _f(conn, "Quarterly Tax Notes 2019.pdf")["decision"] == "not_book"
    # undo walks back one step at a time
    assert review.undo(conn)["file_ids"] == [tax]
    assert _f(conn, "Quarterly Tax Notes 2019.pdf")["decision"] is None
    review.undo(conn)
    assert _f(conn, "the silmarillion.pdf")["decided_work_key"] == "OL1W"
    review.undo(conn)
    assert _f(conn, "the silmarillion.pdf")["decision"] is None
    assert review.undo(conn) is None
    # skip keeps it out of the queue but in the skipped list
    review.decide(conn, tax, "skip")
    q = review.queue(conn)
    assert tax not in q["ids"] and review.queue(conn, "skipped")["ids"] == [tax]
    review.decide(conn, tax, "reset")
    assert tax in review.queue(conn)["ids"]


def test_accept_validation(conn, scanned):  # noqa: F811
    with pytest.raises(review.ReviewError, match="can't be imported"):
        review.decide(conn, _id(conn, "locked.epub"), "accept")
    with pytest.raises(review.ReviewError, match="pick a work"):
        review.decide(conn, _id(conn, "Quarterly Tax Notes 2019.pdf"), "accept")
    with pytest.raises(review.ReviewError, match="unknown work"):
        review.decide(conn, _id(conn, "the silmarillion.pdf"), "accept", work_key="OL404W")
    with pytest.raises(review.ReviewError, match="ISBN is not valid"):
        review.decide(conn, _id(conn, "the silmarillion.pdf"), "accept", overrides={"isbn": "123"})
    with pytest.raises(review.ReviewError, match="unknown action"):
        review.decide(conn, _id(conn, "the silmarillion.pdf"), "explode")


def test_decisions_survive_rescans_but_not_new_contents(conn, scanned):  # noqa: F811
    sil = _id(conn, "the silmarillion.pdf")
    review.decide(conn, sil, "accept")
    _scan(conn, rematch=True)
    _scan(conn, full=True)
    assert _f(conn, "the silmarillion.pdf")["decision"] == "accepted"
    # same name, different file: the old decision doesn't apply any more
    make_pdf(scanned / "the silmarillion.pdf", ["a different scan"])
    old = time.time() - 600
    os.utime(scanned / "the silmarillion.pdf", (old, old))
    _scan(conn)
    assert _f(conn, "the silmarillion.pdf")["decision"] is None


def test_rematch_and_search(conn, scanned):  # noqa: F811
    tax = _id(conn, "Quarterly Tax Notes 2019.pdf")
    c = review.rematch(conn, tax, title="Hamlet", authors="William Shakespeare")
    assert c[0]["ol_key"] == "OL2W" and c[0]["author_sim"] == 100 and c[0]["score"] >= 90
    c = review.rematch(conn, tax, isbn="9780261102217")
    assert c[0]["ol_key"] == "OL262758W" and c[0]["isbn_match"]
    hits = review.search_catalog(conn, tax, "macbeth")
    assert hits[0]["ol_key"] == "OL3W"
    assert _f(conn, "Quarterly Tax Notes 2019.pdf")["decision"] is None   # exploring changes nothing


def test_bulk_accept(conn, drop):  # noqa: F811
    # a few more files that land in Review
    for i, t in enumerate(["the hobbit", "the lord of the rings", "macbeth"]):
        make_pdf(drop / f"more/{t}.pdf", [f"x{i}"])
    old = time.time() - 600
    for f in (drop / "more").iterdir():
        os.utime(f, (old, old))
    _scan(conn)
    assert len(review.queue(conn)["ids"]) == 5
    dry = review.bulk_accept(conn, min_confidence=80)
    assert dry["count"] == 4 and not dry["applied"]                       # everything but the tax notes
    assert _f(conn, "more/the hobbit.pdf")["decision"] is None
    by_author = review.bulk_accept(conn, author_key="OL26320A")
    assert by_author["count"] == 3                                         # Tolkien's three
    r = review.bulk_accept(conn, author_key="OL26320A", dry_run=False)
    assert r["applied"] and len(review.queue(conn)["ids"]) == 2
    # one undo reverts the whole bulk action
    u = review.undo(conn)
    assert u["undone"] == 3 and len(review.queue(conn)["ids"]) == 5
    with pytest.raises(review.ReviewError):
        review.bulk_accept(conn)
    # the floor is the matcher's minimum, however low you ask for
    assert review.bulk_accept(conn, min_confidence=0)["count"] == 4


def test_api_review_flow(env, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("TOMEARR_WORKERS", "0")
    d = env / "ingest"
    make_pdf(d / "the silmarillion.pdf", ["a"])
    make_pdf(d / "Quarterly Tax Notes.pdf", ["b"])
    make_epub(d / "a.epub", isbn="0261102214")
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))

    def wait(client, ids):
        deadline = time.time() + 30
        while time.time() < deadline:
            jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
            if all(jobs[i]["status"] not in ("queued", "running") for i in ids):
                return jobs
            time.sleep(0.1)

    with TestClient(create_app()) as client:
        wait(client, client.post("/api/catalog/import", json={}).json()["job_ids"])
        client.put("/api/settings", json={"ingest_path": str(d), "ingest_interval_min": 0})
        wait(client, [client.post("/api/ingest/scan", json={}).json()["job_id"]])
        q = client.get("/api/review/queue").json()
        assert len(q["ids"]) == 2 and q["counts"]["matched"] == 1
        first = q["ids"][0]
        det = client.get(f"/api/review/{first}").json()
        assert det["selected"] == "OL1W" and det["candidates"][0]["why"]
        r = client.post(f"/api/review/{first}/decide", json={"action": "accept"}).json()
        assert r["remaining"] == 1 and r["next"] == q["ids"][1]
        assert client.post(f"/api/review/{r['next']}/decide", json={"action": "accept"}).status_code == 400
        assert client.post(f"/api/review/{r['next']}/rematch", json={"title": "Hamlet"}).json()[0]["ol_key"] == "OL2W"
        assert client.get(f"/api/review/{r['next']}/search?q=macbeth").json()[0]["ol_key"] == "OL3W"
        assert client.post(f"/api/review/{r['next']}/decide", json={"action": "accept", "work_key": "OL2W"}).json()["remaining"] == 0
        assert client.post("/api/review/undo").json()["undone"] == 1
        assert client.post("/api/review/bulk", json={"min_confidence": 80}).json()["count"] == 0
        assert client.post("/api/review/bulk", json={}).status_code == 400
        assert client.get("/api/review/99999").status_code == 404
        assert client.get("/api/review/queue?which=nope").status_code == 400
        st = client.get("/api/ingest/stats").json()
        assert st["ready"] == 2 and st["review_queue"] == 1
        assert client.get("/api/ingest?decision=accepted").json()["total"] == 1
        assert client.get("/api/ingest?decision=bogus").status_code == 400
