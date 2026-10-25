import { useCallback, useEffect, useState } from "react";
import { api, formatBytes, formatNumber } from "../api.js";
import { JobRow } from "./Catalog.jsx";

const KIND = {
  add: ["New book", "Added to Calibre as a new book"],
  add_format: ["Add EPUB", "EPUB added to a book you already have"],
  already_have: ["Already have it", "Calibre already has this book as EPUB"],
  replace: ["Replace in Calibre", "EPUB in Calibre replaced with this one"],
  cleanup: ["Leftover folders", "Leftovers removed (OPF, images, short txt, empty folders)"],
  delete_duplicate: ["Duplicate", "Identical to a file already in Calibre"],
  unpack: ["Unpack", "Archive unpacked into the ingest folder"],
  pdf_convert: ["Convert PDF", "PDF converted for you to check"],
  pdf_hold: ["PDF", "Held until you opt in"],
  pdf_waiting: ["PDF, check result", "Waiting for your approval"],
  convert_existing: ["Convert", "Book in Calibre converted to EPUB"],
  set_cover: ["Cover", "Cover added from Open Library"],
};
const ORDER = ["add", "add_format", "replace", "convert_existing", "pdf_convert", "unpack", "delete_duplicate", "already_have",
  "cleanup", "pdf_waiting", "pdf_hold"];

const TRASH_GROUPS = ["already_have", "delete_duplicate"];
const CHOICE_LABEL = { calibre: "keeping Calibre's", ingest: "using this one", skip: "deciding later" };

function Row({ label, a, b, better }) {
  return (
    <tr>
      <th>{label}</th>
      <td className={better === "calibre" ? "cmp-better" : ""}>{a}</td>
      <td className={better === "ingest" ? "cmp-better" : ""}>{b}</td>
    </tr>
  );
}

function ComparePanel({ item, onChange }) {
  const [c, setC] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.compare(item.file_id).then(setC).catch((e) => setErr(e.message));
  }, [item.file_id]);
  const choose = async (choice) => {
    setBusy(true);
    try {
      await api.compareChoice(item.file_id, choice);
      onChange(choice !== "skip");     // a real choice happens now; "decide later" just closes
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  if (err) return <div className="notice error">{err}</div>;
  if (!c) return <p className="muted small-print">Comparing{item.format !== "EPUB" ? ` (converting ${item.format} to EPUB first)` : ""}…</p>;
  const A = c.calibre, B = c.ingest;
  const more = (k) => (A[k] === B[k] ? null : A[k] > B[k] ? "calibre" : "ingest");
  const meta = (m) => [m.title, (m.authors || []).join(", "), (m.isbns || [])[0], m.publisher, m.year].filter(Boolean).join(" · ") || "—";
  const s = c.suggestion;
  return (
    <div className="cmp">
      <div className={`notice ${s.choice === "ingest" ? "ok-notice" : ""}`}>
        <strong>
          {s.choice === "ingest" ? "This file looks better." : s.choice === "calibre" ? "Calibre's copy looks better." : "They look about the same."}
        </strong>{" "}
        {s.why.join("; ")}.
      </div>
      <div className="table-wrap">
        <table className="cmp-table">
          <thead>
            <tr>
              <th></th>
              <th>In Calibre (#{c.calibre_id})</th>
              <th>This file{c.converted_for_compare ? ` (${c.format}, converted to compare)` : ""}</th>
            </tr>
          </thead>
          <tbody>
            <Row
              label="Cover"
              a={A.cover_url ? <img className="cmp-cover" src={A.cover_url} alt="Calibre's cover" /> : "none"}
              b={B.cover_url ? <img className="cmp-cover" src={B.cover_url} alt="This file's cover" /> : "none"}
              better={A.has_cover === B.has_cover ? null : A.has_cover ? "calibre" : "ingest"}
            />
            <Row label="Words" a={formatNumber(A.words)} b={formatNumber(B.words)} better={Math.abs(A.words - B.words) > 0.1 * Math.max(A.words, B.words) ? more("words") : null} />
            <Row label="Chapters" a={A.chapters} b={B.chapters} />
            <Row label="Contents" a={A.toc_entries ? `${A.toc_entries} entries` : "none"} b={B.toc_entries ? `${B.toc_entries} entries` : "none"} better={(A.toc_entries > 2) === (B.toc_entries > 2) ? null : A.toc_entries > 2 ? "calibre" : "ingest"} />
            <Row label="Images" a={A.images} b={B.images} />
            <Row label="Fonts" a={A.fonts || "none"} b={B.fonts || "none"} />
            <Row label="Looks like" a={<>{A.origin === "publisher" ? "publisher-made" : A.origin}<div className="muted small-print">{A.origin_why.join("; ")}</div></>}
                 b={<>{B.origin === "publisher" ? "publisher-made" : B.origin}<div className="muted small-print">{B.origin_why.join("; ")}</div></>}
                 better={A.origin === B.origin || [A.origin, B.origin].includes("unknown") ? null : A.origin === "publisher" ? "calibre" : "ingest"} />
            <Row label="EPUB" a={A.epub_version || "?"} b={B.epub_version || "?"} />
            <Row label="Size" a={formatBytes(A.size)} b={formatBytes(B.size)} />
            <Row label="Metadata" a={meta(A.meta)} b={meta(B.meta)} />
            <Row label="Same passage" a={<span className="cmp-sample">{A.sample || "—"}</span>} b={<span className="cmp-sample">{B.sample || "—"}</span>} />
          </tbody>
        </table>
      </div>
      {c.choice && <p className="muted small-print">You chose: {CHOICE_LABEL[c.choice]}.</p>}
      <div className="actions">
        <button className={s.choice !== "ingest" ? "primary" : ""} disabled={busy} onClick={() => choose("calibre")}>
          Keep Calibre's
        </button>
        <button className={s.choice === "ingest" ? "primary" : ""} disabled={busy} onClick={() => choose("ingest")}>
          Use this one
        </button>
        <button disabled={busy} onClick={() => choose("skip")}>
          Decide later
        </button>
      </div>
      <p className="muted small-print">
        “Keep Calibre's” moves this file to the trash right away. “Use this one” replaces the EPUB in Calibre (the old one goes to the
        trash, so it can be undone). Calibre's title and author stay as they are; if Calibre has no cover, it takes
        this file's.
      </p>
    </div>
  );
}

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
  const [open, setOpen] = useState(null);
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
                {TRASH_GROUPS.includes(k) && items.some((x) => sel[x.key]) && (
                  <button className="mini" disabled={running} onClick={() => onRun(items.filter((x) => sel[x.key]).map((x) => x.key), library)}>
                    Move {formatNumber(items.filter((x) => sel[x.key]).length)} checked to the trash now
                  </button>
                )}
              </h3>
              <ul className="plan-items">
                {items.map((i) => (
                  <li key={i.key}>
                    <label>
                      <input type="checkbox" checked={!!sel[i.key]} onChange={(e) => setSel({ ...sel, [i.key]: e.target.checked })} />
                      <span className="plan-what">
                        <span className="ing-path">{i.path || `${i.title} — ${i.authors}`}</span>
                        {i.work_title && <span className="muted"> → {i.work_title}</span>}
                        <span className="plan-steps">
                          {i.steps.join(" · ")}
                          {i.choice && i.kind !== "already_have" && ` · you chose: ${CHOICE_LABEL[i.choice]}`}
                        </span>
                      </span>
                    </label>
                    {(i.kind === "already_have" || i.kind === "replace") && (
                      <button className="link" onClick={() => setOpen(open === i.key ? null : i.key)}>
                        {open === i.key ? "close" : "compare"}
                      </button>
                    )}
                    {open === i.key && (
                      <div className="cmp-wrap">
                        <ComparePanel
                          item={i}
                          onChange={(now) => {
                            setOpen(null);
                            if (now && !running) onRun([i.key], library);
                            else load();
                          }}
                        />
                      </div>
                    )}
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
            {b.rows.some((r) => r.status === "done" && r.action !== "set_cover" && !r.meta?.no_undo) && (
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
                    {r.meta?.cover_added ? " · cover from Open Library" : ""}
                    {r.meta?.calibre_cover_set ? " · cover set in Calibre" : ""}
                    {r.meta?.leftovers?.length > 0 && r.action !== "cleanup" ? ` · ${r.meta.leftovers.length} leftover file${r.meta.leftovers.length === 1 ? "" : "s"} cleaned up` : ""}
                    {r.action === "cleanup" && r.meta?.leftovers ? ` · ${r.meta.leftovers.length} file${r.meta.leftovers.length === 1 ? "" : "s"}` : ""}
                    {r.meta?.no_undo ? " · original deleted" : ""}
                    {r.meta?.kept_formats?.length ? ` · kept ${r.meta.kept_formats.join(", ")} (couldn't back it up)` : ""}
                    {r.error ? ` · ${r.error}` : ""}
                  </span>
                </span>
                {r.status === "done" && r.action !== "set_cover" && !r.meta?.no_undo && (
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
            Calibre connection OK: {formatNumber(conn.books)} book{conn.books === 1 ? "" : "s"} at <code>{conn.target}</code> (calibre {conn.version}).
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
