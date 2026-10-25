"""One book, one Calibre entry: versions under different Open Library records, different ISBNs, or both."""
import os
import time

import pytest

from app import db, identity, importer, library, monitor, review
from samples import make_epub, make_mobi
from test_catalog import _no, _noop, conn  # noqa: F401
from test_phase6 import _books, _cdb, _run, _scan, calibre  # noqa: F401
from test_phase6 import pytestmark as needs_calibre

TOLKIEN, SHAKESPEARE = "OL26320A", "OL9388A"


def _work(conn, key, title, author, subtitle=None):  # noqa: F811
    conn.execute("INSERT INTO works(ol_key, title, subtitle, edition_count, languages) VALUES(?,?,?,5,',eng,')",
                 (key, title, subtitle))
    conn.execute("INSERT INTO work_authors(work_key, author_key) VALUES(?,?)", (key, author))
    conn.commit()


def _settle(d):
    old = time.time() - 3600
    for dp, _d, fs in os.walk(d):
        for f in fs:
            os.utime(os.path.join(dp, f), (old, old))


@pytest.mark.parametrize(("a", "b", "same"), [
    (("The Adventures of Sherlock Holmes", "Arthur Conan Doyle"),
     ("Adventures of Sherlock Holmes (Illustrated)", "Sir Arthur Conan Doyle"), True),
    (("The Hobbit", "J.R.R. Tolkien"), ("The Hobbit: or There and Back Again", "Tolkien, J. R. R."), True),
    (("The Silmarillion (Middle-earth #3)", "J.R.R. Tolkien"), ("The Silmarillion", "J. R. R. Tolkien"), True),
    (("The Colour of Magic", "Terry Pratchett"), ("The Color of Magic", "Terry Pratchett"), True),
    (("Pride & Prejudice", "Jane Austen"), ("Pride and Prejudice: A Novel", "Austen, Jane"), True),
    (("The Brothers Karamazov", "Fyodor Dostoyevsky"), ("The Karamazov Brothers", "Fyodor Dostoyevsky; Ignat Avsey"),
     True),
    (("Star Wars: Thrawn", "Timothy Zahn"), ("Star Wars: Thrawn: Alliances", "Timothy Zahn"), False),
    (("Dune", "Frank Herbert"), ("Dune Messiah", "Frank Herbert"), False),
    (("Foundation", "Isaac Asimov"), ("Foundation and Empire", "Isaac Asimov"), False),
    (("Harry Potter and the Chamber of Secrets", "J.K. Rowling"),
     ("Harry Potter and the Goblet of Fire", "J. K. Rowling"), False),
    (("Hamlet", "William Shakespeare"), ("Hamlet", "Hobbit Fan"), False),
    (("Book 1", "Some Author"), ("Book 2", "Some Author"), False),
])
def test_same_book(a, b, same):
    assert identity.same_book(a[0], a[1], b[0], b[1]) is same


def test_wanted_ignores_books_owned_under_another_record(conn, env):  # noqa: F811
    """Calibre's Hobbit is matched to one record; another record of the same book isn't wanted."""
    from conftest import make_calibre
    d = make_calibre(env / "calibre")
    db.set_meta(conn, "setting_calibre_path", str(d))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    _work(conn, "OL990W", "The Hobbit (Illustrated Edition)", TOLKIEN)
    _work(conn, "OL991W", "The Hobbit Coloring Companion", TOLKIEN)
    monitor.set_monitor(conn, TOLKIEN, "all")
    st = {w["ol_key"]: w["status"] for w in monitor.author_view(conn, TOLKIEN)["works"]}
    assert st["OL990W"] == "owned" and st["OL991W"] != "owned"
    assert "OL990W" not in {w["ol_key"] for w in monitor.wanted(conn)}


@needs_calibre
@pytest.mark.parametrize("calibre", ["direct"], indirect=True)
def test_file_for_a_book_calibre_has_under_another_record(conn, calibre, env):  # noqa: F811
    """Calibre has Hamlet (MOBI, matched to OL2W). A file matched to another Hamlet record adds its EPUB to that
    book instead of creating a second Hamlet."""
    _work(conn, "OL998W", "Hamlet (Arden Shakespeare)", SHAKESPEARE)
    d = env / "in1"
    make_epub(d / "hamlet arden.epub", title="Hamlet", authors=["William Shakespeare"])
    _settle(d)
    db.set_meta(conn, "setting_ingest_path", str(d))
    _scan(conn)
    fid = conn.execute("SELECT id FROM ingest_files").fetchone()[0]
    review.decide(conn, fid, "accept", "OL998W")
    it = importer.plan(conn)["items"][0]
    hamlet = next(i for i, b in _books(calibre).items() if b["title"] == "Hamlet")
    assert it["kind"] == "add_format" and it["calibre_id"] == hamlet
    assert "under another record" in it["steps"][0]
    # "it's a different book": it would be added on its own
    importer.set_compare_choice(conn, fid, "separate")
    assert importer.plan(conn)["items"][0]["kind"] == "add"
    importer.set_compare_choice(conn, fid, None)
    _run(conn)
    assert [b["title"] for b in _books(calibre).values()].count("Hamlet") == 1
    assert "EPUB" in _books(calibre)[hamlet]["formats"]


@needs_calibre
@pytest.mark.parametrize("calibre", ["direct"], indirect=True)
def test_two_records_one_run_imports_the_best_version(conn, calibre, env):  # noqa: F811
    _work(conn, "OL997W", "The Lord of the Rings (50th Anniversary Edition)", TOLKIEN)
    d = env / "in2"
    make_mobi(d / "lotr anniversary.mobi", title="The Lord of the Rings", author="J.R.R. Tolkien")
    make_epub(d / "lotr.epub", title="The Lord of the Rings", authors=["J.R.R. Tolkien"])
    _settle(d)
    db.set_meta(conn, "setting_ingest_path", str(d))
    _scan(conn)
    mobi = conn.execute("SELECT id FROM ingest_files WHERE format='MOBI'").fetchone()[0]
    review.decide(conn, mobi, "accept", "OL997W")          # the MOBI is matched to the other record
    items = importer.plan(conn)["items"]
    kinds = {i["path"]: i["kind"] for i in items}
    assert kinds == {"lotr.epub": "add", "lotr anniversary.mobi": "other_version"}
    assert items[-1]["kind"] == "other_version"             # removed only after the best one is in
    # running only the worse version doesn't trash it: the better one wasn't imported
    r = _run(conn, keys=[f"file:{mobi}"])
    assert r["failed"] == 1 and (d / "lotr anniversary.mobi").exists()
    r = _run(conn, batch=2)
    assert (r["done"], r["failed"]) == (2, 0)
    lotr = [b for b in _books(calibre).values() if b["title"] == "The Lord of the Rings"]
    assert len(lotr) == 1 and lotr[0]["formats"] == ["EPUB"]
    assert not any(d.iterdir())


@needs_calibre
@pytest.mark.parametrize("calibre", ["direct"], indirect=True)
def test_duplicates_already_in_calibre(conn, calibre, env):  # noqa: F811
    src = env / "dupes"
    make_epub(src / "a.epub", title="Unfinished Tales", authors=["J.R.R. Tolkien"])
    make_mobi(src / "b.mobi", title="Unfinished Tales", author="J. R. R. Tolkien")
    make_epub(src / "c.epub", title="The Two Towers", authors=["J.R.R. Tolkien"])
    _cdb(calibre, "add", "--title", "Unfinished Tales", "--authors", "J.R.R. Tolkien", str(src / "a.epub"))
    _cdb(calibre, "add", "-d", "--title", "Unfinished Tales (Illustrated)", "--authors", "J. R. R. Tolkien",
         str(src / "b.mobi"))
    _cdb(calibre, "add", "--title", "The Two Towers", "--authors", "J.R.R. Tolkien", str(src / "c.epub"))
    library.sync(conn, env / "config" / "cache", _noop, _no)
    p = importer.plan_library(conn)
    dups = [i for i in p["items"] if i["kind"] == "remove_duplicate"]
    assert len(dups) == 1 and dups[0]["title"] == "Unfinished Tales (Illustrated)"
    assert dups[0]["select"] is False                       # not added by Tomearr: you decide
    assert dups[0]["move"] == []                            # its MOBI isn't needed next to an EPUB
    r = _run(conn, keys=[dups[0]["key"]], library=True)
    assert r["done"] == 1, importer.history(conn)
    titles = [b["title"] for b in _books(calibre).values()]
    assert titles.count("Unfinished Tales") == 1 and "Unfinished Tales (Illustrated)" not in titles
    assert "The Two Towers" in titles
    h = next(h for h in importer.history(conn) if h["action"] == "remove_duplicate")
    with pytest.raises(importer.ImportError_, match="trash bin"):
        importer.undo(conn, h["id"])
