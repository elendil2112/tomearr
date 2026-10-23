import { useCallback, useEffect, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";

const ACTIVE = ["queued", "running"];

const KIND_LABEL = {
  download: "Download dumps",
  import: "Import into database",
  index: "Build search index",
};

function describe(job) {
  const p = job.progress || {};
  if (job.kind === "download" && p.total) {
    return `${p.file}: ${formatBytes(p.bytes)} of ${formatBytes(p.total)}`;
  }
  if (job.kind === "download" && p.step) return p.step;
  if (job.kind === "import" && p.lines != null) {
    return `${p.phase}: ${formatNumber(p.lines)} lines read, ${formatNumber(p.kept)} kept`;
  }
  if (job.kind === "index" && p.step) return p.step;
  return "";
}

function fraction(job) {
  const p = job.progress || {};
  return job.kind === "download" && p.total ? Math.min(1, p.bytes / p.total) : null;
}

function JobRow({ job, onCancel }) {
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
  const [kinds, setKinds] = useState({ authors: true, works: true });
  const [requireAuthor, setRequireAuthor] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([api.status(), api.jobs()]);
      setStatus(s);
      setJobs(j);
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

  const selected = Object.keys(kinds).filter((k) => kinds[k]);
  const run = (fn) => async () => {
    try {
      await fn({ kinds: selected, require_author: requireAuthor });
      refresh();
    } catch (e) {
      setError(e.message);
    }
  };

  const allDumps = status?.dumps?.every((d) => d.present);

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
                  checked={!!kinds[d.kind]}
                  onChange={(e) => setKinds({ ...kinds, [d.kind]: e.target.checked })}
                />
                <span className="dump-name">{d.kind}</span>
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
          them in that folder as <code>ol_dump_authors.txt.gz</code> and <code>ol_dump_works.txt.gz</code> and use
          “Import only”.
        </p>
        <label className="check">
          <input type="checkbox" checked={requireAuthor} onChange={(e) => setRequireAuthor(e.target.checked)} />
          Skip works that have no author (keeps the database smaller and cleaner)
        </label>
        <div className="actions">
          <button className="primary" disabled={busy || !selected.length} onClick={run(api.build)}>
            Download &amp; build catalog
          </button>
          <button disabled={busy || !allDumps || !selected.length} onClick={run(api.importDumps)}>
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
