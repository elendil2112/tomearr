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
- [ ] **Phase 3: library.** Read your Calibre library (read-only) and match it to catalog works.
- [ ] **Phases 4–6: ingest.** Scan an ingest folder, match files, review queue, convert to EPUB with
      `ebook-convert`, add to Calibre with `calibredb`. Dry run by default, with an undo log. DRM is flagged,
      never stripped.
- [ ] **Phases 7–10: the *arr part.** Monitored authors, wanted list, Prowlarr, download clients, automation.

## Run on Unraid (or any Docker host)

```
docker compose up -d --build
```

Then open `http://<tower-ip>:8080`. Port 8080 taken? Create a `.env` file next to `docker-compose.yml` containing
`TOMEARR_PORT=8222` (or any free port) instead of editing the compose file; `.env` is git-ignored, so `git pull`
stays clean. Then, go to **Catalog**, and click **Download & build catalog**.

| Container path | Purpose |
|---|---|
| `/config` | SQLite database (`tomearr.db`). Put this on cache/SSD. |
| `/data` | Downloaded dump files in `/data/dumps`. |

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
| `GET/PUT /api/settings` | `{"language": "eng"}` |
| `POST /api/catalog/{download,import,build,reindex}` | catalog jobs; import/build take `kinds`, `require_author`, `edition_languages`, `isbn_only` |

## Develop

```
# backend
cd backend && pip install -r requirements.txt pytest
python -m pytest
TOMEARR_CONFIG=./config TOMEARR_DATA=./data uvicorn app.main:create_app --factory --port 8080

# frontend (proxies /api to :8080)
cd frontend && npm install && npm run dev
```

## Layout

```
backend/app/    FastAPI app, job runner, dump importer, search
backend/tests/  pytest suite using small synthetic dumps
frontend/src/   React UI (Search, Author, Work, Catalog pages)
Dockerfile      builds the UI, then a slim Python runtime
```

## License

MIT. See [LICENSE](LICENSE). Open Library data is downloaded at runtime and is not part of this repository.
