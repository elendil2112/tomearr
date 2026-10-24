import gzip
import os
import sys
from pathlib import Path

import orjson
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _line(rtype, key, rec):
    rec = {"key": key, **rec}
    return f"{rtype}\t{key}\t1\t2024-01-01T00:00:00\t{orjson.dumps(rec).decode()}\n"


AUTHORS = [
    ("/authors/OL26320A", {"name": "J.R.R. Tolkien", "birth_date": "3 January 1892", "death_date": "2 September 1973",
                           "alternate_names": ["John Ronald Reuel Tolkien", "Tolkien, J.R.R."]}),
    ("/authors/OL9388A", {"name": "William Shakespeare", "birth_date": "1564", "death_date": "1616"}),
    ("/authors/OL1A", {"personal_name": "Christopher Tolkien"}),
    ("/authors/OL2A", {"name": "   "}),  # unusable: blank name
    ("/authors/OL7A", {"name": "Hobbit Fan"}),
    ("/authors/OL8A", {"name": "Antoine de Saint-Exupéry"}),
]

WORKS = [
    # Decoys come first so they get lower rowids: with text-only ranking they would beat the real Hobbit.
    ("/works/OL6W", {"title": "The Hobbit", "authors": [{"author": {"key": "/authors/OL7A"}}]}),
    ("/works/OL7W", {"title": "The Hobbit Companion", "authors": [{"author": {"key": "/authors/OL7A"}}]}),
    ("/works/OL8W", {"title": "Le Petit Prince", "first_publish_date": "1943",
                     "authors": [{"author": {"key": "/authors/OL8A"}}]}),
    ("/works/OL27448W", {"title": "The Lord of the Rings", "first_publish_date": "1954",
                         "authors": [{"author": {"key": "/authors/OL26320A"}, "type": {"key": "/type/author_role"}}],
                         "subjects": ["Fantasy", "Middle Earth"], "covers": [-1, 9255566]}),
    ("/works/OL262758W", {"title": "The Hobbit", "subtitle": "There and Back Again",
                          "first_publish_date": "September 21, 1937",
                          "authors": [{"author": {"key": "/authors/OL26320A"}}]}),
    ("/works/OL1W", {"title": "The Silmarillion", "authors": [{"author": "/authors/OL26320A"}]}),
    ("/works/OL2W", {"title": "Hamlet", "first_publish_date": "1603",
                     "authors": [{"author": {"key": "/authors/OL9388A"}}]}),
    ("/works/OL3W", {"title": "Macbeth", "authors": [{"author": {"key": "/authors/OL9388A"}}]}),
    ("/works/OL4W", {"title": "Orphan with no author"}),
    ("/works/OL5W", {"authors": [{"author": {"key": "/authors/OL9388A"}}]}),  # no title
]


def _ed(key, work, **rec):
    return (f"/books/{key}", {"works": [{"key": f"/works/{work}"}], **rec})


ENG = [{"key": "/languages/eng"}]
EDITIONS = [
    _ed("OL1M", "OL262758W", title="The Hobbit", isbn_10=["0261102214"], languages=ENG,
        publishers=["HarperCollins"], publish_date="1999", number_of_pages=310, physical_format="Paperback"),
    _ed("OL2M", "OL262758W", title="The Hobbit", isbn_13=["978-0-618-00221-4"], isbn_10=["0618002219"],
        languages=ENG, publish_date="2001", physical_format="E-book"),
    _ed("OL3M", "OL262758W", title="The Hobbit", isbn_10=["0547928228"], languages=ENG),  # bad checksum
    _ed("OL4M", "OL262758W", title="Bilbo le Hobbit", languages=[{"key": "/languages/fre"}]),
    _ed("OL5M", "OL6W", title="The Hobbit", publish_date="2015"),
    _ed("OL6M", "OL7W", title="The Hobbit Companion", isbn_10=["0345339681"], languages=ENG,
        physical_format="Hardcover"),
    _ed("OL7M", "OL8W", title="Le Petit Prince", isbn_10=["2070612759"], languages=[{"key": "/languages/fre"}]),
    _ed("OL8M", "OL27448W", title="The Lord of the Rings", languages=ENG, physical_format="Audio CD"),
    ("/books/OL9M", {"title": "No work key at all", "isbn_10": ["0261102214"]}),
]

RATINGS = (
    "/works/OL262758W\t/books/OL1M\t5\t2023-01-01\n"
    "/works/OL262758W\t\t4\t2023-01-02\n"
    "/works/OL262758W\t\t5\t2023-01-03\n"
    "/works/OL7W,,3,2023-01-04\n"            # comma separated, just in case
    "/works/OL7W\t\t9\t2023-01-05\n"        # out of range, ignored
    "garbage\n"
)

READING_LOG = (
    "/works/OL262758W\t\tWant to Read\t2023-01-01\n"
    "/works/OL262758W\t/books/OL2M\talready-read\t2023-01-01\n"
    "/works/OL262758W\t\tCurrently Reading\t2023-01-01\n"
    "/works/OL27448W\t\tWant to Read\t2023-01-01\n"
    "/works/OL27448W\t\tsomething-else\t2023-01-01\n"
    "/books/OL1M\t\tWant to Read\t2023-01-01\n"   # not a work key
)


def write_dump(path, rtype, rows, junk=True):
    with gzip.open(path, "wb") as fh:
        for key, rec in rows:
            fh.write(_line(rtype, key, rec).encode())
        if junk:
            fh.write(b"garbage line with no tabs\n")
            fh.write(b"/type/redirect\t/x\t1\tt\t{\"location\": \"/y\"}\n")
            fh.write(b"/type/work\t/works/OLBAD\t1\tt\t{not json}\n")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOMEARR_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("TOMEARR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TOMEARR_STATIC", str(tmp_path / "nostatic"))
    (tmp_path / "data" / "dumps").mkdir(parents=True)
    write_dump(tmp_path / "data" / "dumps" / "ol_dump_authors.txt.gz", "/type/author", AUTHORS)
    write_dump(tmp_path / "data" / "dumps" / "ol_dump_works.txt.gz", "/type/work", WORKS)
    write_dump(tmp_path / "data" / "dumps" / "ol_dump_editions.txt.gz", "/type/edition", EDITIONS)
    with gzip.open(tmp_path / "data" / "dumps" / "ol_dump_ratings.txt.gz", "wt") as fh:
        fh.write(RATINGS)
    with gzip.open(tmp_path / "data" / "dumps" / "ol_dump_reading-log.txt.gz", "wt") as fh:
        fh.write(READING_LOG)
    return tmp_path


# --- a small Calibre library (subset of the real metadata.db schema) ----------------------------------------

CALIBRE_SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT 'Unknown',
    sort TEXT, timestamp TIMESTAMP, pubdate TIMESTAMP DEFAULT '0101-01-01 00:00:00+00:00',
    series_index REAL NOT NULL DEFAULT 1.0, author_sort TEXT, isbn TEXT DEFAULT '', lccn TEXT DEFAULT '',
    path TEXT NOT NULL DEFAULT '', flags INTEGER NOT NULL DEFAULT 1, uuid TEXT, has_cover BOOL DEFAULT 0,
    last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:00+00:00');
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT, link TEXT NOT NULL DEFAULT '');
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, author INTEGER NOT NULL);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT NOT NULL,
    uncompressed_size INTEGER NOT NULL, name TEXT NOT NULL);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, type TEXT NOT NULL DEFAULT 'isbn',
    val TEXT NOT NULL);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT NOT NULL);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, lang_code INTEGER NOT NULL,
    item_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, series INTEGER NOT NULL);
"""

# id, title, authors, formats, identifiers, extra
CALIBRE_BOOKS = [
    # ISBN match (ISBN-10 stored in identifiers, as Calibre does)
    (1, "The Hobbit", ["J. R. R. Tolkien"], ["EPUB", "MOBI"], {"isbn": "0261102214"}, {"series": "Middle-earth"}),
    # Open Library id match
    (2, "LOTR omnibus", ["Tolkien, J.R.R."], ["PDF"], {"openlibrary": "OL27448W"}, {}),
    # title + author only, title with subtitle and series noise
    (3, "The Silmarillion: Illustrated (Middle-earth #3)", ["J.R.R. Tolkien"], ["AZW3"], {}, {}),
    (4, "Hamlet, Prince of Denmark", ["William Shakespeare"], ["EPUB"], {}, {"lang": "eng"}),
    # nothing like it in the catalog
    (5, "My Grocery List 2019", ["Joe Nobody"], ["PDF"], {}, {}),
    # no files at all
    (6, "Macbeth", ["William Shakespeare"], [], {}, {}),
]


def make_calibre(lib_dir, books=CALIBRE_BOOKS):
    import sqlite3
    lib_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(lib_dir / "metadata.db")
    c.executescript(CALIBRE_SCHEMA)
    authors: dict[str, int] = {}
    for bid, title, names, formats, ids, extra in books:
        c.execute("INSERT INTO books(id, title, author_sort, last_modified, pubdate) VALUES(?,?,?,?,?)",
                  (bid, title, names[0] if names else "", "2024-01-01 00:00:00+00:00", "0101-01-01 00:00:00+00:00"))
        for n in names:
            if n not in authors:
                authors[n] = len(authors) + 1
                c.execute("INSERT INTO authors(id, name, sort) VALUES(?,?,?)", (authors[n], n, n))
            c.execute("INSERT INTO books_authors_link(book, author) VALUES(?,?)", (bid, authors[n]))
        for f in formats:
            c.execute("INSERT INTO data(book, format, uncompressed_size, name) VALUES(?,?,?,?)", (bid, f, 1000, title))
        for t, v in ids.items():
            c.execute("INSERT INTO identifiers(book, type, val) VALUES(?,?,?)", (bid, t, v))
        if "series" in extra:
            c.execute("INSERT OR IGNORE INTO series(id, name) VALUES(1, ?)", (extra["series"],))
            c.execute("INSERT INTO books_series_link(book, series) VALUES(?, 1)", (bid,))
        if "lang" in extra:
            c.execute("INSERT OR IGNORE INTO languages(id, lang_code) VALUES(1, ?)", (extra["lang"],))
            c.execute("INSERT INTO books_languages_link(book, lang_code) VALUES(?, 1)", (bid,))
    c.commit()
    c.close()
    return lib_dir
