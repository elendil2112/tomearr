import { useState } from "react";
import ReleaseSearch from "./Releases.jsx";

export function Cover({ id, title, size = "S" }) {
  const cls = size === "M" ? "cover large" : "cover";
  if (!id) return <div className={`${cls} blank`} aria-hidden="true" />;
  return (
    <img
      className={cls}
      loading="lazy"
      alt={`Cover of ${title}`}
      src={`https://covers.openlibrary.org/b/id/${id}-${size}.jpg`}
      onError={(e) => {
        e.currentTarget.replaceWith(Object.assign(document.createElement("div"), { className: `${cls} blank` }));
      }}
    />
  );
}

export function OwnedBadge({ owned }) {
  if (owned == null) return null;
  const epub = owned.includes("EPUB");
  return (
    <span className={`owned ${epub ? "epub" : ""}`} title="In your Calibre library">
      Owned{owned.length ? ` · ${owned.join(", ")}` : " · no files"}
    </span>
  );
}

const STATUS_LABEL = {
  wanted: "Wanted",
  in_ingest: "In ingest",
  downloading: "Downloading",
  ignored: "Ignored",
  filtered: "Filtered",
};

export function StatusBadge({ status, why }) {
  if (!STATUS_LABEL[status]) return null;
  return (
    <span className={`status status-${status}`} title={why}>
      {STATUS_LABEL[status]}
    </span>
  );
}

/** The one action that makes sense for a book's current status. */
export function StateButton({ work, onState }) {
  if (!onState || work.status === "owned" || !work.status) return null;
  const [label, next] =
    work.override === "wanted"
      ? ["Don’t want", null]
      : work.override === "ignored"
        ? ["Unignore", null]
        : ["wanted", "in_ingest", "downloading"].includes(work.status)
          ? ["Ignore", "ignored"]
          : ["Want", "wanted"];
  return (
    <button className="mini" onClick={() => onState(work, next)} title={work.status_why}>
      {label}
    </button>
  );
}

const SEARCHABLE = new Set(["wanted", "downloading", "in_ingest", "ignored", "filtered", "unmonitored"]);

export default function WorkList({ works, showAuthors = false, onState, searchable = false, onGrabbed }) {
  const [open, setOpen] = useState(null);
  return (
    <ul className="rows">
      {works.map((w) => (
        <li key={w.ol_key} className={`work-item ${open === w.ol_key ? "open" : ""}`}>
        <div className="row work">
          <Cover id={w.cover_id} title={w.title} />
          <div className="work-body">
            <a className="row-title" href={`#/work/${w.ol_key}`}>
              {w.title}
              {w.subtitle && <span className="subtitle">: {w.subtitle}</span>}
            </a>
            <OwnedBadge owned={w.owned} />
            <StatusBadge status={w.status} why={w.status_why} />
            <div className="row-meta">
              {showAuthors &&
                w.authors?.map((a, i) => (
                  <span key={a.ol_key}>
                    {i > 0 && ", "}
                    <a href={`#/author/${a.ol_key}`}>{a.name}</a>
                  </span>
                ))}
              {showAuthors && w.authors?.length > 0 && w.first_publish_year && " · "}
              {w.first_publish_year}
              {w.edition_count > 0 && (
                <span className="editions">
                  {(showAuthors && w.authors?.length > 0) || w.first_publish_year ? " · " : ""}
                  {w.edition_count} {w.edition_count === 1 ? "edition" : "editions"}
                  {w.ebook_count > 0 && `, ${w.ebook_count} ebook`}
                </span>
              )}
            </div>
            {w.subjects && <div className="row-subjects">{w.subjects.split(" | ").slice(0, 5).join(" · ")}</div>}
          </div>
          {w.status === "filtered" && <span className="muted small-print why">{w.status_why}</span>}
          {searchable && SEARCHABLE.has(w.status) && (
            <button className={`mini ${open === w.ol_key ? "on" : ""}`} onClick={() => setOpen(open === w.ol_key ? null : w.ol_key)}>
              {open === w.ol_key ? "Close" : "Search"}
            </button>
          )}
          <StateButton work={w} onState={onState} />
          <a className="mono ol-link" href={`https://openlibrary.org/works/${w.ol_key}`} target="_blank" rel="noreferrer">
            {w.ol_key}
          </a>
        </div>
        {open === w.ol_key && <ReleaseSearch workKey={w.ol_key} onGrabbed={onGrabbed} />}
        </li>
      ))}
    </ul>
  );
}
