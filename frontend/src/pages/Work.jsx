import { useEffect, useMemo, useState } from "react";
import { api, formatNumber, languageName } from "../api.js";
import { Cover } from "../components/WorkList.jsx";

const FORMAT_LABEL = { ebook: "Ebook", print: "Print", audio: "Audio" };

export default function Work({ olKey }) {
  const [work, setWork] = useState(null);
  const [error, setError] = useState("");
  const [onlyIsbn, setOnlyIsbn] = useState(false);
  const [lang, setLang] = useState("");

  useEffect(() => {
    setWork(null);
    setError("");
    api.work(olKey).then(setWork).catch((e) => setError(e.message));
  }, [olKey]);

  const editions = useMemo(() => {
    if (!work) return [];
    return work.editions.filter((e) => (!onlyIsbn || e.isbns.length) && (!lang || e.language === lang));
  }, [work, onlyIsbn, lang]);

  if (error) return <div className="notice error">{error}</div>;
  if (!work) return <p className="muted">Loading…</p>;

  const { ratings, readers } = work;
  const readerTotal = readers.want + readers.reading + readers.read;

  return (
    <section>
      <a className="back" href="#/search">← Back to search</a>
      <div className="work-head">
        <Cover id={work.cover_id} title={work.title} size="M" />
        <div>
          <h1>
            {work.title}
            {work.subtitle && <span className="subtitle">: {work.subtitle}</span>}
          </h1>
          <p className="lede">
            {work.authors.map((a, i) => (
              <span key={a.ol_key}>
                {i > 0 && ", "}
                <a href={`#/author/${a.ol_key}`}>{a.name}</a>
              </span>
            ))}
            {work.first_publish_year && <span> · first published {work.first_publish_year}</span>}
            {" · "}
            <a className="mono" href={`https://openlibrary.org/works/${work.ol_key}`} target="_blank" rel="noreferrer">
              {work.ol_key}
            </a>
          </p>
        </div>
      </div>

      <WorkMonitor work={work} onChange={setWork} />

      {work.library_books?.length > 0 && (
        <div className="notice ok-notice">
          In your library:{" "}
          {work.library_books
            .map((b) => {
              const f = (b.formats || "").split(",").filter(Boolean);
              return `${b.title} (${f.length ? f.join(", ") : "no files"})`;
            })
            .join("; ")}
          {work.owned && !work.owned.includes("EPUB") && " · not EPUB yet"}
        </div>
      )}

      <div className="tiles">
        <div className="tile">
          <div className="tile-num">{formatNumber(work.edition_count)}</div>
          <div className="tile-label">editions{work.ebook_count ? ` · ${work.ebook_count} ebook` : ""}</div>
        </div>
        <div className="tile">
          <div className="tile-num">{ratings.average ?? "–"}</div>
          <div className="tile-label">
            average rating · {formatNumber(ratings.count)} {ratings.count === 1 ? "rating" : "ratings"}
          </div>
        </div>
        <div className="tile">
          <div className="tile-num">{formatNumber(readerTotal)}</div>
          <div className="tile-label">
            readers · {formatNumber(readers.read)} read, {formatNumber(readers.want)} want to
          </div>
        </div>
      </div>

      {work.subjects && <p className="row-subjects">{work.subjects.split(" | ").join(" · ")}</p>}

      <div className="card">
        <div className="card-head">
          <h2>Editions</h2>
          <div className="filters">
            <select value={lang} onChange={(e) => setLang(e.target.value)} aria-label="Edition language">
              <option value="">All languages</option>
              {work.languages.map((l) => (
                <option key={l} value={l}>
                  {languageName(l)}
                </option>
              ))}
            </select>
            <label className="check inline">
              <input type="checkbox" checked={onlyIsbn} onChange={(e) => setOnlyIsbn(e.target.checked)} />
              With ISBN
            </label>
          </div>
        </div>
        {work.edition_count === 0 ? (
          <p className="muted">
            No editions loaded. Import the editions dump on the <a href="#/catalog">Catalog</a> page.
          </p>
        ) : editions.length === 0 ? (
          <p className="muted">No editions match.</p>
        ) : (
          <div className="table-wrap">
            <table className="editions-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Year</th>
                  <th>Publisher</th>
                  <th>Language</th>
                  <th>Format</th>
                  <th>ISBN</th>
                </tr>
              </thead>
              <tbody>
                {editions.map((e) => (
                  <tr key={e.ol_key}>
                    <td>
                      <a href={`https://openlibrary.org/books/${e.ol_key}`} target="_blank" rel="noreferrer">
                        {e.title || work.title}
                      </a>
                      {e.pages && <span className="muted"> · {e.pages} pp</span>}
                    </td>
                    <td>{e.publish_year || ""}</td>
                    <td>{e.publisher || ""}</td>
                    <td>{e.language ? languageName(e.language) : ""}</td>
                    <td>{FORMAT_LABEL[e.format] || ""}</td>
                    <td className="mono">
                      {e.isbns.map((i) => (
                        <div key={i}>{i}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {work.editions.length < work.edition_count && (
          <p className="muted small-print">
            Showing the first {work.editions.length} of {formatNumber(work.edition_count)} editions (ebooks first, then
            newest).
          </p>
        )}
      </div>
    </section>
  );
}

const STATUS_TEXT = {
  wanted: "Wanted",
  in_ingest: "Wanted, and a file for it is waiting in the ingest folder",
  ignored: "Ignored",
  filtered: "Not wanted: filtered out by your profile",
  unmonitored: "Not wanted",
};

function WorkMonitor({ work, onChange }) {
  const s = work.monitor_status;
  if (!s || s.status === "owned") return null;
  const set = async (state) => {
    await api.setWorkState(work.ol_key, state);
    onChange({ ...work, monitor_status: (await api.work(work.ol_key)).monitor_status });
  };
  const wanted = s.status === "wanted" || s.status === "in_ingest";
  return (
    <div className={`notice ${wanted ? "ok-notice" : ""}`} style={wanted ? {} : { background: "var(--card)", borderColor: "var(--line)" }}>
      <b>{STATUS_TEXT[s.status]}</b>
      {s.status !== "in_ingest" && s.why && <span className="muted"> ({s.why})</span>}
      <span style={{ marginLeft: 12 }}>
        {s.override ? (
          <button className="mini" onClick={() => set(null)}>
            {s.override === "wanted" ? "Don’t want" : "Unignore"}
          </button>
        ) : wanted ? (
          <button className="mini" onClick={() => set("ignored")}>Ignore</button>
        ) : (
          <button className="mini" onClick={() => set("wanted")}>Want this book</button>
        )}
      </span>
    </div>
  );
}
