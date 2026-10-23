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
]

WORKS = [
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
    return tmp_path
