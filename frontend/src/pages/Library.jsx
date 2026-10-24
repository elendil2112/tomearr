import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatNumber } from "../api.js";
import { JobRow } from "./Catalog.jsx";

const FILTERS = [
  ["all", "All", "books"],
  ["not_epub", "Not EPUB yet", "not_epub"],
  ["low", "Check match", "low"],
  ["unmatched", "Unmatched", "unmatched"],
  ["manual", "Manual", "manual"],
];

const METHOD_LABEL = { isbn: "ISBN", openlibrary: "OL id", title_author: "title + author", manual: "manual" };
const PAGE = 200;

function Formats({ formats }) {
  if (!formats.length) return <span className="fmt none">no files</span>;
  return formats.map((f) => (
    <span key={f} className={`fmt ${f === "EPUB" ? "epub" : ""}`}>
      {f}
    </span>
  ));
}

function MatchFixer({ book, onDone }) {
  // Start from the main title: every search word must match, so subtitles and series notes get in the way.
  const [q, setQ] = useState(() => book.title.split(/\s*[,:;([]/)[0]);
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!q.trim()) return setResults([]);
    const t = setTimeout(() => {
      api.searchWorks(`${q}`, "all").then((r) => setResults(r.slice(0, 8))).catch(() => {});
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  const act = (fn) => async () => {
    setBusy(true);
    try {
      await fn();
      onDone();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixer">
      <input className="search compact" value={q} onChange={(e) => setQ(e.target.value)} autoFocus aria-label="Search the catalog" />
      <ul className="fixer-results">
        {results.map((w) => (
          <li key={w.ol_key}>
            <button disabled={busy} onClick={act(() => api.setMatch(book.calibre_id, w.ol_key))}>
              <strong>{w.title}</strong>
              <span className="muted">
                {" "}
                {w.authors?.map((a) => a.name).join(", ")}
                {w.first_publish_year ? ` · ${w.first_publish_year}` : ""} · {w.ol_key}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <div className="actions">
        <button disabled={busy} onClick={act(() => api.setMatch(book.calibre_id, null))}>
          Not in the catalog
        </button>
        {book.match_locked ? (
          <button disabled={busy} onClick={act(() => api.clearMatch(book.calibre_id))}>
            Back to automatic
          </button>
        ) : null}
        <button onClick={onDone}>Cancel</button>
      </div>
    </div>
  );
}

function BookRow({ book, onChanged }) {
  const [fixing, setFixing] = useState(false);
  const low = book.work_key && !book.match_locked && book.match_confidence < 90;
  return (
    <li className="lib-book">
      <div className="lib-main">
        <div className="lib-title">
          {book.title}
          {book.series && (
            <span className="muted">
              {" "}
              · {book.series}
              {book.series_index != null ? ` #${Number(book.series_index)}` : ""}
            </span>
          )}
        </div>
        <div className="lib-meta">
          <Formats formats={book.formats} />
          {book.work_key ? (
            <a className={`match ${low ? "low" : ""}`} href={`#/work/${book.work_key}`}>
              → {book.work_title || book.work_key}
              {book.work_year ? ` (${book.work_year})` : ""}
              <span className="match-why">
                {METHOD_LABEL[book.match_method] || book.match_method}
                {book.match_method !== "manual" ? ` · ${book.match_confidence}%` : ""}
              </span>
            </a>
          ) : (
            <span className="match none">{book.match_locked ? "marked not in catalog" : "no match"}</span>
          )}
          <button className="link" onClick={() => setFixing(!fixing)}>
            {fixing ? "close" : "fix"}
          </button>
        </div>
      </div>
      {fixing && (
        <MatchFixer
          book={book}
          onDone={() => {
            setFixing(false);
            onChanged();
          }}
        />
      )}
    </li>
  );
}

export default function Library() {
  const [stats, setStats] = useState(null);
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState({ total: 0, items: [] });
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const reqId = useRef(0);

  const loadStats = useCallback(() => api.libraryStats().then(setStats).catch((e) => setError(e.message)), []);

  const load = useCallback(
    async (offset = 0) => {
      const id = ++reqId.current;
      try {
        const r = await api.library({ q, filter, offset, limit: PAGE });
        if (id !== reqId.current) return;
        setPage((p) => (offset ? { total: r.total, items: [...p.items, ...r.items] } : r));
      } catch (e) {
        setError(e.message);
      }
    },
    [q, filter],
  );

  useEffect(() => {
    loadStats();
    api.jobs().then((js) => setJob(js.find((j) => j.kind === "library_sync") || null)).catch(() => {});
  }, [loadStats]);

  useEffect(() => {
    const t = setTimeout(() => load(0), 200);
    return () => clearTimeout(t);
  }, [load]);

  const running = job && ["queued", "running"].includes(job.status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      const js = await api.jobs().catch(() => []);
      const j = js.find((x) => x.id === job.id);
      if (j) setJob(j);
      if (j && !["queued", "running"].includes(j.status)) {
        loadStats();
        load(0);
      }
    }, 1200);
    return () => clearInterval(t);
  }, [running, job?.id, load, loadStats]);

  const sync = (rematch) => async () => {
    try {
      const { job_id } = await api.librarySync(rematch);
      setJob({ id: job_id, kind: "library_sync", status: "queued", progress: {} });
    } catch (e) {
      setError(e.message);
    }
  };

  const refresh = () => {
    loadStats();
    load(0);
  };

  // Group the current page by author for display.
  const groups = [];
  for (const b of page.items) {
    const key = b.authors || "Unknown author";
    if (!groups.length || groups[groups.length - 1].key !== key) groups.push({ key, books: [] });
    groups[groups.length - 1].books.push(b);
  }

  const never = stats && !stats.synced_at;

  return (
    <section>
      <h1>Library</h1>
      <p className="lede">
        Your Calibre library, read without changing anything, and matched to the catalog.
        {stats?.synced_at && ` Last synced ${stats.synced_at} UTC from ${stats.path}.`}
      </p>
      {error && <div className="notice error">{error}</div>}
      {never && (
        <div className="notice">
          Not synced yet. Check the Calibre folder in <a href="#/settings">Settings</a>, then sync.
        </div>
      )}

      <div className="tiles">
        <div className="tile">
          <div className="tile-num">{formatNumber(stats?.books)}</div>
          <div className="tile-label">books in Calibre</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(stats?.matched)}</div>
          <div className="tile-label">
            matched to the catalog{stats?.books ? ` · ${Math.round((100 * stats.matched) / stats.books)}%` : ""}
          </div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(stats?.not_epub)}</div>
          <div className="tile-label">not EPUB yet</div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Sync</h2>
          <div className="actions">
            <button className="primary" disabled={running} onClick={sync(false)}>
              Sync now
            </button>
            <button disabled={running} onClick={sync(true)} title="Re-run matching for every book except manual matches">
              Sync and rematch all
            </button>
          </div>
        </div>
        {job ? (
          <ul className="jobs">
            <JobRow job={job} onCancel={(id) => api.cancel(id)} />
          </ul>
        ) : (
          <p className="muted small-print">Changed and unmatched books are matched again on every sync. Manual matches are never changed.</p>
        )}
      </div>

      <div className="tabs" role="tablist">
        {FILTERS.map(([id, label, statKey]) => (
          <button key={id} role="tab" aria-selected={filter === id} className={filter === id ? "on" : ""} onClick={() => setFilter(id)}>
            {label} <span className="count">{formatNumber(stats?.[statKey])}</span>
          </button>
        ))}
      </div>
      <input className="search compact" placeholder="Filter by title or author" value={q} onChange={(e) => setQ(e.target.value)} />

      {page.items.length === 0 ? (
        <p className="muted">{stats?.books ? "No books match." : "Nothing here yet."}</p>
      ) : (
        <>
          {groups.map((g, i) => (
            <div key={`${g.key}-${i}`} className="lib-group">
              <h3 className="lib-author">{g.key}</h3>
              <ul className="rows">
                {g.books.map((b) => (
                  <BookRow key={b.calibre_id} book={b} onChanged={refresh} />
                ))}
              </ul>
            </div>
          ))}
          {page.items.length < page.total && (
            <div className="actions center">
              <button onClick={() => load(page.items.length)}>
                Show more ({formatNumber(page.total - page.items.length)} left)
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
