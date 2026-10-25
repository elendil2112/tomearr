import { useEffect, useState } from "react";
import { api, formatBytes } from "../api.js";

const age = (d) => (d == null ? "" : d < 1 ? "today" : d < 60 ? `${d}d` : d < 730 ? `${Math.round(d / 30)}mo` : `${Math.round(d / 365)}y`);

/** Search Prowlarr for one book and grab a release. */
export default function ReleaseSearch({ workKey, onGrabbed }) {
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  const [showRejected, setShowRejected] = useState(false);
  const [busy, setBusy] = useState(null);
  const [grabbed, setGrabbed] = useState({});

  const run = () => {
    setRes(null);
    setErr("");
    api.releases(workKey).then(setRes).catch((e) => setErr(e.message));
  };
  useEffect(run, [workKey]);

  const grab = async (r) => {
    setBusy(r.id);
    setErr("");
    try {
      const g = await api.grab(workKey, r.id);
      setGrabbed({ ...grabbed, [r.id]: g });
      onGrabbed?.(g);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  };

  if (err && !res)
    return (
      <div className="releases">
        <div className="notice error">
          {err} {/Prowlarr isn't set up|Deluge isn't|SABnzbd isn't/.test(err) && <a href="#/settings">Settings</a>}
        </div>
        <button className="mini" onClick={run}>Try again</button>
      </div>
    );
  if (!res) return <div className="releases muted small-print">Searching your indexers… (this can take a few seconds)</div>;

  const ok = res.releases.filter((r) => !r.rejected.length);
  const rejected = res.releases.filter((r) => r.rejected.length);
  const list = showRejected ? res.releases : ok;
  return (
    <div className="releases">
      {err && <div className="notice error">{err}</div>}
      {res.active.length > 0 && (
        <p className="small-print ok">Already downloading: {res.active.map((g) => g.title).join("; ")}</p>
      )}
      <p className="muted small-print">
        {ok.length} {ok.length === 1 ? "release" : "releases"} for “{res.queries[0]}” in {res.took}s
        {rejected.length > 0 && (
          <>
            {" · "}
            <button className="link" onClick={() => setShowRejected(!showRejected)}>
              {showRejected ? "hide" : "show"} {rejected.length} passed over
            </button>
          </>
        )}
        {" · "}
        <button className="link" onClick={run}>search again</button>
      </p>
      {list.length === 0 && <p className="muted small-print">Nothing that looks like this book.</p>}
      <ul className="rel-list">
        {list.map((r, i) => {
          const g = grabbed[r.id];
          return (
            <li key={r.id} className={r.rejected.length ? "rejected" : i === 0 ? "best" : ""}>
              <div className="rel-main">
                <span className="rel-title">{r.title}</span>
                <span className="rel-meta">
                  {r.format && <span className={`fmt ${r.format === "EPUB" ? "epub" : ""}`}>{r.format}</span>}
                  <span>{r.protocol === "usenet" ? "Usenet" : "Torrent"}</span>
                  <span>{r.indexer}</span>
                  {r.size > 0 && <span>{formatBytes(r.size)}</span>}
                  {r.protocol === "torrent" && r.seeders != null && <span>{r.seeders} seeders</span>}
                  {r.age_days != null && <span>{age(r.age_days)}</span>}
                  {r.info_url && (
                    <a href={r.info_url} target="_blank" rel="noreferrer">
                      details
                    </a>
                  )}
                </span>
                {(r.rejected.length > 0 || r.reasons.length > 0) && (
                  <span className="rel-why">
                    {r.rejected.length > 0 && <span className="bad">{r.rejected.join(", ")}</span>}
                    {r.rejected.length > 0 && r.reasons.length > 0 && " · "}
                    {r.reasons.join(" · ")}
                  </span>
                )}
              </div>
              {!r.rejected.length && <span className="rel-score" title="Tomearr's score">{Math.round(r.score)}</span>}
              {g ? (
                <span className="ok small-print">Sent to {g.client === "deluge" ? "Deluge" : "SABnzbd"}</span>
              ) : (
                <button className={`mini ${i === 0 && !r.rejected.length ? "primary" : ""}`} disabled={busy != null} onClick={() => grab(r)}>
                  {busy === r.id ? "Sending…" : "Grab"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
