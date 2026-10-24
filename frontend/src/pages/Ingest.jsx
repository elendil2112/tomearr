import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";
import { JobRow } from "./Catalog.jsx";

const BUCKETS = [
  ["all", "All"],
  ["matched", "Matched"],
  ["review", "Review"],
  ["unmatched", "Unmatched"],
  ["duplicate", "Duplicates"],
  ["drm", "DRM"],
  ["archive", "Archives"],
  ["comic", "Comics"],
  ["error", "Errors"],
];

const METHOD = { isbn: "ISBN", openlibrary: "OL id", title_author: "title + author", edition_title: "edition title" };
const SOURCE = (s) =>
  !s ? "" : s === "embedded" ? "file metadata" : s === "sidecar" ? "metadata.opf" : s === "isbn" ? "ISBN in file" :
  s.startsWith("filename:") ? `file name (${s.slice(9)})` : s;
const PAGE = 200;

function FileRow({ f }) {
  const [open, setOpen] = useState(false);
  const slash = f.path.lastIndexOf("/");
  const folder = slash >= 0 ? f.path.slice(0, slash + 1) : "";
  const name = f.path.slice(slash + 1);
  const low = f.work_key && f.match_confidence < 90;
  return (
    <li className="ing-file">
      <div className="ing-name">
        <span className={`fmt ${f.format === "EPUB" ? "epub" : ""}`}>{f.format || "?"}</span>
        <span className="ing-path">
          {folder && <span className="muted">{folder}</span>}
          {name}
        </span>
        <span className="muted small-print ing-size">{formatBytes(f.size)}</span>
      </div>
      <div className="lib-meta">
        {f.work_key ? (
          <a className={`match ${low ? "low" : ""}`} href={`#/work/${f.work_key}`}>
            → {f.work_title || f.work_key}
            {f.work_year ? ` (${f.work_year})` : ""}
            <span className="match-why">
              {METHOD[f.match_method] || f.match_method} · {f.match_confidence}% · from {SOURCE(f.match_source)}
            </span>
          </a>
        ) : f.kind === "ebook" ? (
          <span className="match none">no match{f.title ? ` for “${f.title}”` : ""}</span>
        ) : f.kind === "archive" ? (
          <span className="match none">
            archive{f.entries?.length ? ` with ${f.entries.length} ebook file${f.entries.length === 1 ? "" : "s"} inside` : ""}; unpacked at import
          </span>
        ) : f.kind === "comic" ? (
          <span className="match none">comic book archive; not imported into Calibre</span>
        ) : null}
        {f.drm && <span className="issue">DRM{f.drm_detail ? `: ${f.drm_detail}` : ""}</span>}
        {f.duplicate_of && (
          <span className="issue warn">
            duplicate of{" "}
            {f.duplicate_of.startsWith("calibre:") ? `a book already in Calibre (#${f.duplicate_of.slice(8)})` : f.duplicate_of.slice(7)}
          </span>
        )}
        {f.owned_formats.length > 0 && !f.duplicate_of && (
          <span className="owned">Calibre has {f.owned_formats.join(", ")}</span>
        )}
        {f.error && <span className="issue">{f.error}</span>}
        {f.decision && (
          <span className={`decision d-${f.decision}`}>
            {f.decision === "accepted" ? `accepted${f.decided_title && f.decided_work_key !== f.work_key ? ` as ${f.decided_title}` : ""}` :
             f.decision === "not_book" ? "not a book" : "skipped"}
          </span>
        )}
        {f.kind === "ebook" && (
          <a className="link" href={`#/review/${f.id}`}>
            review
          </a>
        )}
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "less" : "details"}
        </button>
      </div>
      {open && (
        <div className="ing-details">
          {f.meta && (
            <p>
              <strong>In the file</strong> ({f.meta_source}): {f.meta.title || "no title"}
              {f.meta.authors?.length ? ` · ${f.meta.authors.join(", ")}` : ""}
              {f.meta.isbns?.length ? ` · ISBN ${f.meta.isbns.join(", ")}` : ""}
              {f.meta.language ? ` · ${f.meta.language}` : ""}
              {f.meta.series ? ` · ${f.meta.series}${f.meta.series_index != null ? ` #${f.meta.series_index}` : ""}` : ""}
            </p>
          )}
          {f.sidecar && (
            <p>
              <strong>metadata.opf</strong>: {f.sidecar.title} {f.sidecar.authors?.length ? `· ${f.sidecar.authors.join(", ")}` : ""}
            </p>
          )}
          {f.title && (
            <p>
              <strong>Matched using</strong>: “{f.title}”{f.authors ? ` by ${f.authors}` : ""}
              {f.isbn ? ` · ISBN ${f.isbn}` : ""}
            </p>
          )}
          {f.entries?.length > 0 && (
            <p>
              <strong>Inside the archive</strong>: {f.entries.join(", ")}
            </p>
          )}
          {f.candidates?.length > 0 && (
            <>
              <strong>Candidates</strong>
              <ul className="cands">
                {f.candidates.map((c) => (
                  <li key={c.ol_key}>
                    <a href={`#/work/${c.ol_key}`}>{c.title}</a>
                    <span className="muted">
                      {" "}
                      {c.authors?.join(", ")}
                      {c.year ? ` · ${c.year}` : ""}
                      {c.score != null ? ` · ${Math.round(c.score)}%` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="muted small-print mono">sha256 {f.sha256}</p>
        </div>
      )}
    </li>
  );
}

export default function Ingest() {
  const [stats, setStats] = useState(null);
  const [bucket, setBucket] = useState("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState({ total: 0, items: [] });
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const reqId = useRef(0);

  const loadStats = useCallback(() => api.ingestStats().then(setStats).catch((e) => setError(e.message)), []);
  const load = useCallback(
    async (offset = 0) => {
      const id = ++reqId.current;
      try {
        const r = await api.ingest({ bucket, q, offset, limit: PAGE });
        if (id !== reqId.current) return;
        setPage((p) => (offset ? { total: r.total, items: [...p.items, ...r.items] } : r));
      } catch (e) {
        setError(e.message);
      }
    },
    [bucket, q],
  );

  useEffect(() => {
    loadStats();
    api.jobs().then((js) => setJob(js.find((j) => j.kind === "ingest_scan") || null)).catch(() => {});
  }, [loadStats]);
  useEffect(() => {
    const t = setTimeout(() => load(0), 200);
    return () => clearTimeout(t);
  }, [load]);

  // Follow the latest scan, including ones the scheduler starts on its own.
  const running = job && ["queued", "running"].includes(job.status);
  useEffect(() => {
    const t = setInterval(async () => {
      const js = await api.jobs().catch(() => []);
      const j = js.find((x) => x.kind === "ingest_scan");
      if (!j) return;
      const finished = job && job.id === j.id && running && !["queued", "running"].includes(j.status);
      const newer = !job || j.id !== job.id;
      setJob(j);
      if (finished || (newer && j.status === "done")) {
        loadStats();
        load(0);
      }
    }, running ? 1200 : 10000);
    return () => clearInterval(t);
  }, [running, job, load, loadStats]);

  const scan = (opts) => async () => {
    try {
      const { job_id } = await api.ingestScan(opts);
      setJob({ id: job_id, kind: "ingest_scan", status: "queued", progress: {} });
    } catch (e) {
      setError(e.message);
    }
  };

  const b = stats?.buckets || {};
  const last = stats?.last_scan;

  return (
    <section>
      <h1>Ingest</h1>
      <p className="lede">
        Every ebook in <code>{stats?.path}</code>, identified and matched. Report only for now: nothing here is moved
        or changed.
      </p>
      {error && <div className="notice error">{error}</div>}

      {stats?.review_queue > 0 && (
        <a className="notice rv-cta" href="#/review">
          <strong>{formatNumber(stats.review_queue)}</strong> file{stats.review_queue === 1 ? "" : "s"} need a decision.
          Review them →
        </a>
      )}

      <div className="tiles">
        <div className="tile">
          <div className="tile-num">{formatNumber(stats?.files)}</div>
          <div className="tile-label">files · {formatNumber(stats?.ready)} ready to import</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(b.matched)}</div>
          <div className="tile-label">matched</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber((b.review || 0) + (b.unmatched || 0))}</div>
          <div className="tile-label">to review or unmatched</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(b.duplicate)}</div>
          <div className="tile-label">duplicates · {formatNumber(stats?.owned)} already owned</div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Scan</h2>
          <div className="actions">
            <button className="primary" disabled={running} onClick={scan({})}>
              Scan now
            </button>
            <button disabled={running} onClick={scan({ rematch: true })} title="Match every file again, e.g. after updating the catalog">
              Re-match all
            </button>
            <button disabled={running} onClick={scan({ full: true })} title="Read every file again, not just new or changed ones">
              Full rescan
            </button>
          </div>
        </div>
        {job && (
          <ul className="jobs">
            <JobRow job={job} onCancel={(id) => api.cancel(id)} />
          </ul>
        )}
        {last && (
          <p className="muted small-print">
            Last scan {stats.scanned_at} UTC: {formatNumber(last.files)} files, {formatNumber(last.read)} read
            {last.removed ? `, ${formatNumber(last.removed)} gone` : ""}
            {last.settling ? `, ${formatNumber(last.settling)} still arriving (next scan)` : ""}
            {last.partial ? `, ${formatNumber(last.partial)} partial downloads skipped` : ""}
            {last.ignored ? `, ${formatNumber(last.ignored)} non-book files ignored` : ""}.{" "}
            <a href="#/settings">Interval and folder</a>
          </p>
        )}
      </div>

      <div className="tabs" role="tablist">
        {BUCKETS.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={bucket === id} className={bucket === id ? "on" : ""} onClick={() => setBucket(id)}>
            {label} <span className="count">{formatNumber(id === "all" ? stats?.files : b[id])}</span>
          </button>
        ))}
      </div>
      <input className="search compact" placeholder="Filter by file name, title or author" value={q} onChange={(e) => setQ(e.target.value)} />

      {page.items.length === 0 ? (
        <p className="muted">{stats?.files ? "No files in this bucket." : "Nothing scanned yet. Drop books in the folder, or press Scan now."}</p>
      ) : (
        <>
          <ul className="rows">
            {page.items.map((f) => (
              <FileRow key={f.id} f={f} />
            ))}
          </ul>
          {page.items.length < page.total && (
            <div className="actions center">
              <button onClick={() => load(page.items.length)}>Show more ({formatNumber(page.total - page.items.length)} left)</button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
