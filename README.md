# Tomearr

A self-hosted ebook catalog and organizer in the style of the *arr apps. So far it is the catalog: it downloads
Open Library's bulk data dumps (authors, works, editions, ratings, reading log), loads them into a local SQLite
database, and gives you a fast search UI with ISBN lookup and popularity ranking, with no API, no rate limits and
no outside dependency once the data is in.

![Searching the catalog](docs/search.png)

> **Status: early.** Phase 1 (authors and works) has been run on the real dumps. Phase 2 (editions, ISBNs,
> popularity) has been tested against synthetic dumps and a 1M-work / 3M-edition synthetic benchmark, not yet the
> real editions dump. Expect rough edges and please open issues.

## Roadmap

The full phased plan lives in the project notes; in short:

- [x] **Phase 1: catalog.** Dump downloader, streaming importer, FTS5 search, React UI.
- [x] **Phase 2: catalog quality.** Editions and ISBNs, edition counts, ratings and reading-log popularity,
      popularity-aware ranking, language filter, ISBN lookup, work detail page.
- [x] **Phase 3: library.** Read your Calibre library (read-only) and match it to catalog works.
- [x] **Phase 4: ingest scanner.** Identify and match every file in a drop folder, on an interval. Report only.
- [x] **Phase 5: review.** Keyboard-driven review of everything the matcher wasn't sure about.
- [ ] **Phase 6: import.** Convert to EPUB with
      `ebook-convert`, add to Calibre with `calibredb`. Dry run by default, with an undo log. DRM is flagged,
      never stripped.
- [ ] **Phases 7–10: the *arr part.** Monitored authors, wanted list, Prowlarr, download clients, automation.

## Run on Unraid (or any Docker host)

```
docker compose up -d --build
```

Then open `http://<tower-ip>:8222`. Port 8222 taken? Create a `.env` file next to `docker-compose.yml` containing
`TOMEARR_PORT=<free port>` instead of editing the compose file; `.env` is git-ignored, so `git pull`
stays clean. Then, go to **Catalog**, and click **Download & build catalog**.

| Container path | Purpose |
|---|---|
| `/config` | SQLite database (`tomearr.db`). Put this on cache/SSD. |
| `/data` | Downloaded dump files in `/data/dumps`. |
| `/calibre` | Your Calibre library (the folder containing `metadata.db`). Mount it read-only (`:ro`). |
| `/ingest` | Drop folder for new and unsorted ebooks (read-only for now). Set `INGEST_FOLDER` in `.env` to change it. |

Already have the dumps (the Open Library site offers torrents)? Put them in `/data/dumps` named
`ol_dump_<kind>.txt.gz` (no date, e.g. `ol_dump_editions.txt.gz`), then click **Import only**.

**Upgrading from Phase 1:** the database migrates itself on start. On the Catalog page only the missing dumps
(editions, ratings, reading log) are ticked; click **Download & build catalog** to fetch and import just those.
Search keeps working on the old index until the rebuild finishes.

### What gets downloaded

| File | Compressed size (per Open Library) | Used for |
|---|---|---|
| authors | about 0.5 GB | required |
| works | about 2.9 GB | required |
| editions | about 9 GB | ISBNs, languages, edition counts |
| ratings | a few MB | popularity |
| reading-log | about 65 MB | popularity |

The editions dump is the big one, and its table will be the largest part of `tomearr.db`. **Lite mode** (on the
Catalog page) keeps only editions with an ISBN and/or in chosen languages. Editions with no language listed are
always kept, since many ebooks leave it blank.

Imports show live progress as a percentage of the compressed file read; downloads resume if interrupted.

## Your library (Calibre)

Mount your Calibre library at `/calibre`. The compose file mounts `/mnt/user/appdata/calibre/Calibre Library`
read-only; for a different folder, put `CALIBRE_LIBRARY=/path/to/library` in `.env` rather than editing the
compose file. Then check it under **Settings → Test connection**, then **Library → Sync now**.

- Tomearr never opens Calibre's `metadata.db` directly. It copies it (and any `-wal` file) into
  `/config/cache`, checks the copy is consistent, and reads only the copy. It takes no lock on your library and
  is safe while Calibre, the content server or Calibre-Web is running. If Calibre writes during the copy it
  retries.
- Each book is matched to a catalog work: Calibre's `openlibrary` identifier first, then ISBN, then title plus
  author (fuzzy), then the titles of that author's editions (catches translations, e.g. a Swedish "Lida" is
  King's "Misery"). Matches show their method and a confidence; **Check match** lists anything under 90%.
- Tomearr expects messy records and works around them rather than asking you to clean Calibre first: author
  strings are cleaned (Calibre's `Tolkien| J.R.R.`, roles like "(Editor)", stray quotes, "Unknown"), titles are
  read with and without volume numbers, series notes and an author name tacked on, and a title match with a
  clearly different author is never accepted.
- Problems in a Calibre record itself are flagged under **Calibre problems**, for example an ISBN that belongs to a
  different book (the title and author win, and the ISBN is reported). Writing corrections back into Calibre
  comes with Phase 6.
- Use **fix** on a book to pick the right work or mark it "not in the catalog". Manual matches are never
  changed by a sync; "Back to automatic" undoes one.
- Re-syncing only re-matches books that changed in Calibre or are still unmatched (or everything, with
  **Sync and rematch all**). Books deleted from Calibre disappear from Tomearr's list.
- Search results, author pages and work pages show an **Owned** badge with the formats you have.

## Ingest folder

Tomearr scans the ingest folder on an interval (default every 5 minutes; **Settings**) and identifies every file.
Nothing is moved or changed yet; that's the import step.

- **Every format.** Files are identified by their contents, not just the extension. Embedded metadata is read from
  EPUB/KEPUB, MOBI/AZW/AZW3/PRC, PDF (document info, XMP, and an ISBN found on the first pages), FB2 (also
  zipped), DOCX/ODT, HTMLZ/TXTZ and Gutenberg-style TXT. Other formats (LIT, LRF, DJVU, CHM, RTF, DOC, KFX, ...)
  are matched from the file name, and from Calibre's `ebook-meta` once that is in the image. A `metadata.opf`
  next to a book is used too.
- **File names.** Several readings are tried ("Author - Title", "Title - Author", "Title (Author)",
  "Author/Title/" folders, release tags and site names stripped) and the one that matches the catalog best wins.
- **Buckets:** Matched (90%+), Review (60–89%), Unmatched, Duplicates, DRM, Archives, Comics, Errors.
- **Duplicates** are found by SHA-256, within the folder and against your Calibre library (only Calibre files of
  the same size are hashed). Matched books you already own show which formats Calibre has.
- **DRM** (Adobe ADEPT, Apple FairPlay, Kindle/Mobipocket, KFX, password-protected PDFs) is detected and flagged.
  Tomearr never removes DRM. Font obfuscation in EPUBs is not DRM and isn't flagged.
- **Interval scans are cheap:** only new or changed files (size or modified time) are read. Files modified in the
  last 2 minutes are skipped until the next scan, since they're probably still downloading or copying, and so are
  partial downloads (`.part`, `.!qB`, ...). Each file is read in a worker process with a time limit, so one broken
  file can't stall a scan. Polling is used rather than file-change notifications, which don't reliably reach a
  container from Unraid user shares.
- **Re-match all** re-runs matching without re-reading files (after a catalog update, say); **Full rescan** re-reads
  everything.

## Review

**Review** walks through the files the matcher wasn't sure about (the Review and Unmatched buckets), one at a
time, most promising first. For each file it shows what's known about it (and where that came from: the file,
a metadata.opf, the file name, or your edits) next to the candidate works, with covers, authors, year, editions,
whether you already own it, a score, and why: "ISBN … is an edition of this book", "title 100% similar", "same
author".

| Key | |
|---|---|
| Enter / A | accept the highlighted match |
| ↑ ↓ / J K | move between matches |
| 1 – 9 | accept that match |
| N | not a book |
| S | skip for now (it moves to Skipped) |
| E | edit the details (title, authors, series, ISBN, language); Enter finds matches for them |
| / | search the catalog; Enter searches, Enter again accepts |
| U | undo (a bulk action undoes as one) |
| ← → | previous / next file |
| ? | show the keys |

- **Bulk:** accept every match at N% or better, or every match to the current candidate's author. Both show how
  many files they'll touch first, and one **U** undoes them.
- Decisions are stored apart from the automatic match, so rescans and re-matching never overwrite them. If a
  file's contents change, its decision is cleared.
- Your edits are saved with the decision; the import step will write them into Calibre.
- **Ready to import** = files you accepted plus confident automatic matches you didn't overrule.

## How search works

- Authors and works are loaded from the dumps. Works with no author are skipped by default (toggle on the Catalog page).
- SQLite FTS5 indexes author names (including alternate names) and work titles plus author names.
- Every word you type is a prefix match and all words must match, so `tolkien hobbit` finds The Hobbit.
- **Popularity** per work = editions + 3 × ratings + 2 × "already read" + "currently reading" + "want to read".
  The works index is stored in popularity order, so a query pulls its most popular matches first, then they are
  reranked by how much of the title the query covers plus a log-scaled popularity term. An exact title match still
  beats a famous book with a partial match, but among equal matches the famous one wins.
- **Language filter** (next to the search box, default English) hides works with no edition in that language.
  Works whose editions list no language at all are always shown.
- **ISBN lookup:** type or paste an ISBN-10 or ISBN-13 (dashes and an `ISBN` prefix are fine). ISBN-10s are stored
  as ISBN-13; values with a bad checksum are dropped at import.
- Author results are ranked by how many works they have.

### API

| Route | |
|---|---|
| `GET /api/search/works?q=&lang=` | ranked works (`lang`: MARC code like `eng`, or `all`; defaults to the saved setting) |
| `GET /api/search/authors?q=` | authors |
| `GET /api/isbn/{isbn}` | editions and works for an ISBN |
| `GET /api/works/{key}` | work detail with editions, ISBNs, ratings, reader counts |
| `GET /api/authors/{key}` | author with works |
| `GET/PUT /api/settings` | `language`, `calibre_path`, `ingest_path`, `ingest_interval_min`, `ingest_settle_sec`, `library_sync_interval_min` |
| `POST /api/library/test` | check a Calibre folder (`{"path": ...}`, defaults to the setting) |
| `POST /api/library/sync` | sync job (`{"rematch": true}` re-matches everything except manual matches) |
| `GET /api/library?q=&filter=&offset=&limit=` | owned books; `filter`: all, matched, unmatched, low, not_epub, issues, manual |
| `GET /api/library/stats` | counts by match method and format |
| `PUT/DELETE /api/library/{calibre_id}/match` | set a manual match (`{"work_key": "OL…W"}` or `null`) / clear it |
| `POST /api/ingest/test` | check the ingest folder (`{"path": ...}`) |
| `POST /api/ingest/scan` | scan job (`{"full": true}` re-reads every file, `{"rematch": true}` re-matches) |
| `GET /api/ingest?bucket=&q=&offset=&limit=` | scanned files with metadata, match, candidates |
| `GET /api/ingest/stats` | bucket and format counts, review queue, ready to import, last scan summary |
| `GET /api/review/queue?which=` | file ids: `queue` (default), `skipped`, `accepted`, `not_book`, `matched` |
| `GET /api/review/{id}` | a file with its best reading and candidates (with reasons) |
| `POST /api/review/{id}/decide` | `{"action": "accept"\|"not_book"\|"skip"\|"reset", "work_key": ..., "overrides": {...}}` |
| `POST /api/review/{id}/rematch` | candidates for edited details (`title`, `authors`, `isbn`) |
| `GET /api/review/{id}/search?q=` | catalog search, as candidates for this file |
| `POST /api/review/bulk` | `{"min_confidence": 80}` or `{"author_key": "OL…A"}`, `dry_run` (default true) |
| `POST /api/review/undo` | undo the last decision or bulk action |
| `POST /api/catalog/{download,import,build,reindex}` | catalog jobs; import/build take `kinds`, `require_author`, `edition_languages`, `isbn_only` |

## Develop

```
# backend
cd backend && pip install -r requirements.txt pytest
python -m pytest
TOMEARR_CONFIG=./config TOMEARR_DATA=./data uvicorn app.main:create_app --factory --port 8222

# frontend (proxies /api to :8222)
cd frontend && npm install && npm run dev
```

## Layout

```
backend/app/    FastAPI app, job runner, scheduler, dump importer, search, Calibre reader, matcher, ingest scanner
backend/tests/  pytest suite using small synthetic dumps
frontend/src/   React UI (Search, Author, Work, Library, Ingest, Review, Catalog, Settings pages)
Dockerfile      builds the UI, then a slim Python runtime
```

## License

MIT. See [LICENSE](LICENSE). Open Library data is downloaded at runtime and is not part of this repository.
