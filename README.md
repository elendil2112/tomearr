# Tomearr

A self-hosted ebook catalog and organizer in the style of the *arr apps. **Phase 1** (this build) is the catalog:
it downloads Open Library's bulk data dumps, loads them into a local SQLite database, and gives you a fast
search UI with no API, no rate limits and no outside dependency once the data is in.

![Searching the catalog](docs/search.png)

> **Status: early.** Phase 1 has been tested against small synthetic dumps only, not a full Open Library import, and
> the Docker image has not yet been built outside a development sandbox. Expect rough edges and please open issues.

## Roadmap

- [x] **Phase 1: catalog.** Dump downloader, streaming importer, FTS5 search, React UI.
- [ ] **Phase 2: scan and match.** Scan an ingest folder (EPUB, MOBI, AZW, PDF), read embedded metadata and ISBNs,
      match files to the catalog with a confidence score. Report only, nothing moves.
- [ ] **Phase 3: review UI.** Matched, needs-review and unmatched queues.
- [ ] **Phase 4: convert and organize.** Convert to EPUB with Calibre's `ebook-convert` (DRM-protected files are
      flagged, never stripped), then add books to your Calibre library with `calibredb`. Calibre's library is the
      only destination, so there is one copy of each book. Dry run by default, with an undo log.
- [ ] **Phase 5: extras.** Scheduled dump refreshes, duplicate detection, optional Hardcover lookups for series data.

## Run on Unraid (or any Docker host)

```
docker compose up -d --build
```

Then open `http://<tower-ip>:8080`, go to **Catalog**, and click **Download & build catalog**.

| Container path | Purpose |
|---|---|
| `/config` | SQLite database (`tomearr.db`). Put this on cache/SSD. |
| `/data` | Downloaded dump files in `/data/dumps`. |

Already have the dumps (the Open Library site offers torrents)? Put them in `/data/dumps` named
`ol_dump_authors.txt.gz` and `ol_dump_works.txt.gz`, then click **Import only**.

### What gets downloaded

| File | Compressed size (per Open Library) |
|---|---|
| authors | about 0.5 GB |
| works | about 2.9 GB |

Editions (about 9 GB) are not used yet. They will matter in a later phase for ISBN matching.

Expect the first import to take a while on a full dump (tens of millions of works). I have only tested with a small
synthetic sample, so I can't give you real timings or the final database size yet. Run it once and the Catalog page
shows live progress; the download resumes if interrupted.

## How search works

- Authors and works are loaded from the dumps. Works with no author are skipped by default (toggle on the Catalog page).
- SQLite FTS5 indexes author names (including alternate names) and work titles plus author names.
- Every word you type is a prefix match and all words must match, so `tolkien hobbit` finds The Hobbit.
- Author results are ranked by how many works they have. Book results are ranked by text relevance only; the
  dumps used here carry no popularity data, so a famous title and an obscure one with the same words can sort oddly.

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
frontend/src/   React UI (Search, Author, Catalog pages)
Dockerfile      builds the UI, then a slim Python runtime
```

## License

MIT. See [LICENSE](LICENSE). Open Library data is downloaded at runtime and is not part of this repository.
