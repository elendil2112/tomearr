import { useCallback, useEffect, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";
import { JobRow } from "./Catalog.jsx";

const KIND = {
  add: ["New book", "Added to Calibre as a new book"],
  add_format: ["Add EPUB", "EPUB added to a book you already have"],
  already_have: ["Already have it", "Calibre already has this book as EPUB"],
  delete_duplicate: ["Duplicate", "Identical to a file already in Calibre"],
  unpack: ["Unpack", "Archive unpacked into the ingest folder"],
  pdf_convert: ["Convert PDF", "PDF converted for you to check"],
  pdf_hold: ["PDF", "Held until you opt in"],
  pdf_waiting: ["PDF, check result", "Waiting for your approval"],
  convert_existing: ["Convert", "Book in Calibre converted to EPUB"],
};
const ORDER = ["add", "add_format", "convert_existing", "pdf_convert", "unpack", "delete_duplicate", "already_have",
  "pdf_waiting", "pdf_hold"];

function PdfCard({ item, onChange }) {
  const [prev, setPrev] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (item.kind === "pdf_waiting") api.pdfPreview(item.file_id).then(setPrev).catch((e) => setErr(e.message));
  }, [item]);
  const act = (a) => api.pdf(item.file_id, a).then(onChange).catch((e) => setErr(e.message));
  return (
    <div className="pdf-card">
      <div className="ing-path">{item.path}</div>
      {item.work_title && <div className="muted small-print">→ {item.work_title}</div>}
      {err && <div className="issue">{err}</div>}
      {item.kind === "pdf_hold" && (
        <div className="actions">
          <button onClick={() => act("convert")}>Convert to EPUB for checking</button>
        </div>
      )}
      {item.kind === "pdf_convert" && <p className="muted small-print">Will be converted on the next import run. Nothing is added to Calibre yet.</p>}
      {item.kind === "pdf_waiting" && (
        <>
          {prev && (
            <>
              <p className="muted small-print">
                Converted EPUB: {formatBytes(prev.size)}, {prev.files} sections.{" "}
                <a href={`/api/import/pdf/${item.file_id}/epub`}>Download it</a> to check in a reader.
              </p>
              <pre className="pdf-sample">{prev.sample || "(no text came out of the conversion; probably a scanned PDF)"}</pre>
            </>
          )}
          <div className="actions">
            <button className="primary" onClick={() => act("approve")}>Looks good, import it</button>
            <button onClick={() => act("reject")}>Reject</button>
          </div>
        </>
      )}
    </div>
  );
}

function PlanView({ library, onRun, running }) {
  const [plan, setPlan] = useState(null);
  const [sel, setSel] = useState({});
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api
      .importPlan(library)
      .then((p) => {
        setPlan(p);
        setSel(Object.fromEntries(p.items.map((i) => [i.key, i.select])));
      })
      .catch((e) => setErr(e.message));
  }, [library]);
  useEffect(load, [load, running]);

  if (err) return <div className="notice error">{err}</div>;
  if (!plan) return <p className="muted">Working out the plan…</p>;
  const pdfs = plan.items.filter((i) => i.kind.startsWith("pdf_") && i.kind !== "pdf_convert");
  const doable = plan.items.filter((i) => !pdfs.includes(i));
  const chosen = doable.filter((i) => sel[i.key]).map((i) => i.key);
  const groups = ORDER.map((k) => [k, doable.filter((i) => i.kind === k)]).filter(([, v]) => v.length);

  return (
    <>
      {!plan.calibre_tools && (
        <div className="notice error">Calibre's tools aren't installed in this container, so nothing can be converted or added.</div>
      )}
      <div className="card">
        <div className="card-head">
          <h2>{library ? "Convert books already in Calibre" : "Ready to import"}</h2>
          <div className="actions">
            <button className="primary" disabled={running || !chosen.length} onClick={() => onRun(chosen, library)}>
              {library ? "Convert" : "Import"} {formatNumber(chosen.length)}
            </button>
          </div>
        </div>
        <p className="muted small-print">
          Dry run: nothing has happened yet. Writing to <code>{plan.target}</code>. Originals go to the trash (kept for the
          number of days in Settings) and every step can be undone below.
        </p>
        {groups.length === 0 ? (
          <p className="muted">{library ? "Every book in Calibre already has an EPUB (or only formats you keep)." : "Nothing is ready. Accept files on the Review page, or wait for confident matches from the next scan."}</p>
        ) : (
          groups.map(([k, items]) => (
            <div key={k} className="plan-group">
              <h3>
                {KIND[k]?.[0] || k} <span className="count">{items.length}</span>
                <button className="link" onClick={() => setSel({ ...sel, ...Object.fromEntries(items.map((i) => [i.key, !items.every((x) => sel[x.key])])) })}>
                  {items.every((x) => sel[x.key]) ? "none" : "all"}
                </button>
              </h3>
              <ul className="plan-items">
                {items.map((i) => (
                  <li key={i.key}>
                    <label>
                      <input type="checkbox" checked={!!sel[i.key]} onChange={(e) => setSel({ ...sel, [i.key]: e.target.checked })} />
                      <span className="plan-what">
                        <span className="ing-path">{i.path || `${i.title} — ${i.authors}`}</span>
                        {i.work_title && <span className="muted"> → {i.work_title}</span>}
                        <span className="plan-steps">{i.steps.join(" · ")}</span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </div>
      {!library && pdfs.length > 0 && (
        <div className="card">
          <h2>PDFs</h2>
          <p className="muted small-print">
            PDF to EPUB conversion is often rough, so each PDF needs your OK: convert it, check the result, then approve.
            The PDF itself stays in the ingest folder until the approved EPUB is in Calibre.
          </p>
          {pdfs.map((i) => (
            <PdfCard key={i.key} item={i} onChange={load} />
          ))}
        </div>
      )}
    </>
  );
}

function History({ refreshKey }) {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const load = useCallback(() => api.importHistory().then(setRows).catch((e) => setErr(e.message)), []);
  useEffect(() => {
    load();
  }, [load, refreshKey]);
  const undo = (fn) => fn().then(load).catch((e) => setErr(e.message));
  const batches = [];
  for (const r of rows) {
    if (!batches.length || batches[batches.length - 1].batch !== r.batch) batches.push({ batch: r.batch, rows: [] });
    batches[batches.length - 1].rows.push(r);
  }
  return (
    <div className="card">
      <h2>History</h2>
      {err && <div className="notice error">{err}</div>}
      {!rows.length && <p className="muted">Nothing imported yet.</p>}
      {batches.map((b) => (
        <div key={b.batch ?? "none"} className="plan-group">
          <h3>
            Run #{b.batch} <span className="muted small-print">{b.rows[b.rows.length - 1].at} UTC</span>
            {b.rows.some((r) => r.status === "done") && (
              <button className="link" onClick={() => undo(() => api.importUndoBatch(b.batch))}>
                undo this run
              </button>
            )}
          </h3>
          <ul className="plan-items">
            {b.rows.map((r) => (
              <li key={r.id} className={`hist hist-${r.status}`}>
                <span className={`badge badge-${r.status === "done" ? "done" : r.status === "failed" ? "failed" : ""}`}>{r.status}</span>
                <span className="plan-what">
                  <span className="ing-path">{r.path || `Calibre book #${r.calibre_id}`}</span>
                  <span className="plan-steps">
                    {KIND[r.action]?.[1] || r.action}
                    {r.calibre_id ? ` (#${r.calibre_id})` : ""}
                    {r.source_format && r.added_format && r.source_format !== "EPUB" ? ` · converted from ${r.source_format}` : ""}
                    {r.removed?.length ? ` · removed ${r.removed.map((x) => x.format).join(", ")}` : ""}
                    {r.meta?.kept_formats?.length ? ` · kept ${r.meta.kept_formats.join(", ")} (couldn't back it up)` : ""}
                    {r.error ? ` · ${r.error}` : ""}
                  </span>
                </span>
                {r.status === "done" && (
                  <button className="link" onClick={() => undo(() => api.importUndo(r.id))}>
                    undo
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export default function Import() {
  const [tab, setTab] = useState("ingest");
  const [job, setJob] = useState(null);
  const [conn, setConn] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api.jobs().then((js) => setJob(js.find((j) => j.kind === "calibre_import") || null)).catch(() => {});
    api.calibreTest().then(setConn).catch((e) => setConn({ ok: false, message: e.message }));
  }, []);
  const running = job && ["queued", "running"].includes(job.status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      const js = await api.jobs().catch(() => []);
      const j = js.find((x) => x.id === job.id);
      if (j) setJob(j);
      if (j && !["queued", "running"].includes(j.status)) setTick((n) => n + 1);
    }, 1200);
    return () => clearInterval(t);
  }, [running, job?.id]);

  const run = async (keys, library) => {
    const { job_id } = await api.importRun(keys, library);
    setJob({ id: job_id, kind: "calibre_import", status: "queued", progress: {} });
  };

  return (
    <section>
      <h1>Import</h1>
      <p className="lede">Convert approved books to EPUB and add them to Calibre. You see the plan first; nothing happens until you press Import.</p>
      {conn &&
        (conn.ok ? (
          <div className="notice ok-notice">
            Calibre connection OK: {formatNumber(conn.books)} books at <code>{conn.target}</code> (calibre {conn.version}).
          </div>
        ) : (
          <div className="notice error">
            Can't write to Calibre: {conn.message} <a href="#/settings">Settings</a>
          </div>
        ))}
      {job && (
        <ul className="jobs card">
          <JobRow job={job} onCancel={(id) => api.cancel(id)} />
        </ul>
      )}
      <div className="tabs" role="tablist">
        <button className={tab === "ingest" ? "on" : ""} onClick={() => setTab("ingest")}>
          From the ingest folder
        </button>
        <button className={tab === "library" ? "on" : ""} onClick={() => setTab("library")}>
          Books already in Calibre
        </button>
        <button className={tab === "history" ? "on" : ""} onClick={() => setTab("history")}>
          History
        </button>
      </div>
      {tab === "ingest" && <PlanView key={`i${tick}`} library={false} onRun={run} running={running} />}
      {tab === "library" && <PlanView key={`l${tick}`} library onRun={run} running={running} />}
      {tab === "history" && <History refreshKey={tick} />}
    </section>
  );
}
