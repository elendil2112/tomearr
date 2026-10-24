import { useCallback, useEffect, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";

const ACTIVE = ["queued", "running"];

const KIND_LABEL = {
  download: "Download dumps",
  import: "Import into database",
  index: "Build search index",
  library_sync: "Sync Calibre library",
  ingest_scan: "Scan ingest folder",
  calibre_import: "Import into Calibre",
};

const DUMP_INFO = {
  authors: "about 0.5 GB · required",
  works: "about 2.9 GB · required",
  editions: "about 9 GB · ISBNs, languages, edition counts",
  ratings: "a few MB · popularity",
  "reading-log": "about 65 MB · popularity",
};

const pct = (p) => (p.total ? ` (${Math.floor((100 * p.bytes) / p.total)}%)` : "");

function describe(job) {
  const p = job.progress || {};
  if (job.kind === "download" && p.total) {
    return `${p.file}: ${formatBytes(p.bytes)} of ${formatBytes(p.total)}`;
  }
  if (job.kind === "download" && p.step) return p.step;
  if (job.kind === "import" && p.lines != null) {
    return `${p.phase}: ${formatNumber(p.lines)} lines read, ${formatNumber(p.kept)} kept${pct(p)}`;
  }
  if (job.kind === "index" && p.step) return p.step;
  if (job.kind === "library_sync" && p.total) return `${formatNumber(p.done)} of ${formatNumber(p.total)} books`;
  if (job.kind === "ingest_scan" && p.total) return `${p.step}: ${formatNumber(p.done)} of ${formatNumber(p.total)} files`;
  if (job.kind === "ingest_scan" && p.step) return p.step;
  if (job.kind === "calibre_import" && p.total != null)
    return p.step === "done" ? `${p.done} done, ${p.failed} failed` : `${formatNumber(p.done)} of ${formatNumber(p.total)}${p.current ? `: ${p.current}` : ""}`;
  if (job.kind === "calibre_import" && p.step) return p.step;
  if (job.kind === "library_sync" && p.step) return p.step;
  return "";
}

function fraction(job) {
  const p = job.progress || {};
  if (["library_sync", "ingest_scan", "calibre_import"].includes(job.kind) && p.total) return Math.min(1, (p.done || 0) / p.total);
  return (job.kind === "download" || job.kind === "import") && p.total ? Math.min(1, p.bytes / p.total) : null;
}

export function JobRow({ job, onCancel }) {
  const active = ACTIVE.includes(job.status);
  const frac = fraction(job);
  return (
    <li className={`job job-${job.status}`}>
      <div className="job-main">
        <span className="job-title">{KIND_LABEL[job.kind] || job.kind}</span>
        <span className={`badge badge-${job.status}`}>{job.status}</span>
        {active && (
          <button className="link" onClick={() => onCancel(job.id)}>
            cancel
          </button>
        )}
      </div>
      {job.status === "running" && (
        <div className={`bar ${frac == null ? "indeterminate" : ""}`}>
          <span style={frac != null ? { width: `${frac * 100}%` } : undefined} />
        </div>
      )}
      <div className="job-sub">{job.message || (job.status === "running" ? describe(job) : "")}</div>
    </li>
  );
}

export default function Catalog() {
  const [status, setStatus] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState("");
  const [kinds, setKinds] = useState(null); // chosen once the dump list loads
  const [requireAuthor, setRequireAuthor] = useState(true);
  const [isbnOnly, setIsbnOnly] = useState(false);
  const [editionLangs, setEditionLangs] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([api.status(), api.jobs()]);
      setStatus(s);
      setJobs(j);
      // Default to whatever hasn't been downloaded yet; if everything is there, select all (a refresh).
      setKinds((k) => {
        if (k) return k;
        const missing = s.dumps.filter((d) => !d.present).map((d) => d.kind);
        const pick = missing.length ? missing : s.dumps.map((d) => d.kind);
        return Object.fromEntries(s.dumps.map((d) => [d.kind, pick.includes(d.kind)]));
      });
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const busy = jobs.some((j) => ACTIVE.includes(j.status));

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, busy ? 1500 : 6000);
    return () => clearInterval(t);
  }, [refresh, busy]);

  const selected = Object.keys(kinds || {}).filter((k) => kinds[k]);
  const run = (fn) => async () => {
    try {
      await fn({
        kinds: selected,
        require_author: requireAuthor,
        isbn_only: isbnOnly,
        edition_languages: editionLangs.split(/[\s,]+/).filter(Boolean),
      });
      refresh();
    } catch (e) {
      setError(e.message);
    }
  };

  const selectedPresent = selected.length > 0 && status?.dumps?.filter((d) => kinds[d.kind]).every((d) => d.present);
  const lite = status?.editions_lite;

  return (
    <section>
      <h1>Catalog</h1>
      <p className="lede">
        Tomearr keeps its own copy of the Open Library catalog, so searches never touch an outside API and never get
        rate limited.
      </p>

      {error && <div className="notice error">{error}</div>}

      <div className="tiles">
        <div className="tile">
          <div className="tile-num">{formatNumber(status?.counts.authors)}</div>
          <div className="tile-label">authors</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(status?.counts.works)}</div>
          <div className="tile-label">works</div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(status?.counts.editions)}</div>
          <div className="tile-label">
            editions · {formatNumber(status?.counts.isbns)} ISBNs
            {lite && (lite.isbn_only || lite.languages.length > 0) ? " · lite" : ""}
          </div>
        </div>
        <div className="tile">
          <div className="tile-num small">{status?.searchable ? "Ready" : "Not built"}</div>
          <div className="tile-label">search index{status?.indexed_at ? ` · ${status.indexed_at} UTC` : ""}</div>
        </div>
      </div>

      <div className="card">
        <h2>Dump files</h2>
        <ul className="dumps">
          {status?.dumps?.map((d) => (
            <li key={d.kind}>
              <label>
                <input
                  type="checkbox"
                  checked={!!kinds?.[d.kind]}
                  onChange={(e) => setKinds({ ...kinds, [d.kind]: e.target.checked })}
                />
                <span className="dump-name">{d.kind.replace("-", " ")}</span>
                <span className="muted dump-info">{DUMP_INFO[d.kind]}</span>
              </label>
              <span className={d.present ? "ok" : "muted"}>
                {d.present
                  ? formatBytes(d.size)
                  : d.partial_size
                  ? `partial, ${formatBytes(d.partial_size)} so far`
                  : "not downloaded"}
              </span>
            </li>
          ))}
        </ul>
        <p className="muted small-print">
          Files live in <code>{status?.dump_dir}</code>. Already have the dumps (for example from the torrent)? Drop
          them in that folder named like <code>ol_dump_editions.txt.gz</code> (no date) and use “Import only”. You can
          import the extra dumps on their own later; the search index is rebuilt after every import.
        </p>
        <label className="check">
          <input type="checkbox" checked={requireAuthor} onChange={(e) => setRequireAuthor(e.target.checked)} />
          Skip works that have no author (keeps the database smaller and cleaner)
        </label>
        {kinds?.editions && (
          <fieldset className="lite">
            <legend>Editions lite mode (optional, saves disk space)</legend>
            <label className="check">
              <input type="checkbox" checked={isbnOnly} onChange={(e) => setIsbnOnly(e.target.checked)} />
              Only keep editions that have an ISBN
            </label>
            <label className="field">
              <span>Only keep editions in these languages</span>
              <input
                type="text"
                placeholder="e.g. eng, spa (blank keeps all)"
                value={editionLangs}
                onChange={(e) => setEditionLangs(e.target.value)}
              />
            </label>
            <p className="muted small-print">
              Editions with no language listed are always kept. Dropping editions also lowers edition counts, which
              feed the popularity ranking.
            </p>
          </fieldset>
        )}
        <div className="actions">
          <button className="primary" disabled={busy || !selected.length} onClick={run(api.build)}>
            Download &amp; build catalog
          </button>
          <button disabled={busy || !selectedPresent} onClick={run(api.importDumps)}>
            Import only
          </button>
          <button disabled={busy} onClick={() => api.reindex().then(refresh).catch((e) => setError(e.message))}>
            Rebuild index
          </button>
        </div>
      </div>

      <div className="card">
        <h2>Activity</h2>
        {jobs.length === 0 ? (
          <p className="muted">Nothing has run yet.</p>
        ) : (
          <ul className="jobs">
            {jobs.map((j) => (
              <JobRow key={j.id} job={j} onCancel={(id) => api.cancel(id).then(refresh)} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
