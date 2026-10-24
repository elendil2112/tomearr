import { useEffect, useState } from "react";
import { api, formatNumber, LANGUAGES } from "../api.js";

export default function Settings() {
  const [settings, setSettings] = useState(null);
  const [path, setPath] = useState("");
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .settings()
      .then((s) => {
        setSettings(s);
        setPath(s.calibre_path);
      })
      .catch((e) => setError(e.message));
  }, []);

  const save = async (body, label) => {
    setError("");
    try {
      const s = await api.saveSettings(body);
      setSettings(s);
      setPath(s.calibre_path);
      setSaved(label);
      setTimeout(() => setSaved(""), 2500);
    } catch (e) {
      setError(e.message);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.libraryTest(path));
    } catch (e) {
      setTest({ ok: false, message: e.message });
    } finally {
      setTesting(false);
    }
  };

  if (!settings) return error ? <div className="notice error">{error}</div> : <p className="muted">Loading…</p>;

  return (
    <section>
      <h1>Settings</h1>
      {error && <div className="notice error">{error}</div>}

      <div className="card">
        <h2>Calibre library</h2>
        <p className="muted small-print">
          The folder that contains <code>metadata.db</code>, as the container sees it. With the default compose file
          that is <code>/calibre</code>. To read your library, Tomearr copies <code>metadata.db</code> and reads the copy,
          so syncing never locks it. Adding books is set up below.
        </p>
        <label className="field">
          <span>Library folder</span>
          <input type="text" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} />
        </label>
        <div className="actions">
          <button disabled={testing} onClick={runTest}>
            {testing ? "Testing…" : "Test connection"}
          </button>
          <button className="primary" disabled={path === settings.calibre_path} onClick={() => save({ calibre_path: path }, "calibre")}>
            Save
          </button>
          {saved === "calibre" && <span className="ok small-print">Saved</span>}
        </div>
        {test &&
          (test.ok ? (
            <div className="notice ok-notice">
              Found {formatNumber(test.books)} books by {formatNumber(test.authors)} authors.{" "}
              {Object.entries(test.formats)
                .map(([f, n]) => `${formatNumber(n)} ${f}`)
                .join(", ")}
              .
            </div>
          ) : (
            <div className="notice error">{test.message}</div>
          ))}
        <NumberSetting
          label="Resync Calibre every (minutes, 0 = off)"
          value={settings.library_sync_interval_min}
          onSave={(v) => save({ library_sync_interval_min: v }, "libint")}
          saved={saved === "libint"}
        />
      </div>

      <CalibreWriteSettings settings={settings} save={save} saved={saved} />

      <IngestSettings settings={settings} save={save} saved={saved} />

      <div className="card">
        <h2>Search</h2>
        <label className="field">
          <span>Default book language</span>
          <select value={settings.language} onChange={(e) => save({ language: e.target.value }, "language")}>
            <option value="all">All languages</option>
            {LANGUAGES.map(([code, name]) => (
              <option key={code} value={code}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {saved === "language" && <span className="ok small-print">Saved</span>}
      </div>
    </section>
  );
}

function NumberSetting({ label, value, onSave, saved, hint }) {
  const [v, setV] = useState(String(value));
  useEffect(() => setV(String(value)), [value]);
  const n = Number(v);
  return (
    <label className="field">
      <span>{label}</span>
      <span className="inline-field">
        <input type="number" min="0" value={v} onChange={(e) => setV(e.target.value)} />
        <button disabled={v === "" || Number.isNaN(n) || n === value} onClick={() => onSave(n)}>
          Save
        </button>
        {saved && <span className="ok small-print">Saved</span>}
      </span>
      {hint && <span className="muted small-print">{hint}</span>}
    </label>
  );
}

function IngestSettings({ settings, save, saved }) {
  const [path, setPath] = useState(settings.ingest_path);
  const [test, setTest] = useState(null);
  useEffect(() => setPath(settings.ingest_path), [settings.ingest_path]);

  const runTest = async () => {
    setTest(null);
    try {
      setTest(await api.ingestTest(path));
    } catch (e) {
      setTest({ ok: false, message: e.message });
    }
  };

  return (
    <div className="card">
      <h2>Ingest folder</h2>
      <p className="muted small-print">
        Where new and unsorted ebooks land, as the container sees it (default <code>/ingest</code>). Scans only read
        it; an import moves the originals it has added to Calibre into the trash.
      </p>
      <label className="field">
        <span>Folder</span>
        <input type="text" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} />
      </label>
      <div className="actions">
        <button onClick={runTest}>Test folder</button>
        <button className="primary" disabled={path === settings.ingest_path} onClick={() => save({ ingest_path: path }, "ingest")}>
          Save
        </button>
        {saved === "ingest" && <span className="ok small-print">Saved</span>}
      </div>
      {test &&
        (test.ok ? (
          <div className="notice ok-notice">
            {formatNumber(test.files)} files:{" "}
            {Object.entries(test.by_kind)
              .map(([k, n]) => `${formatNumber(n)} ${k}`)
              .join(", ")}
            .
          </div>
        ) : (
          <div className="notice error">{test.message}</div>
        ))}
      <NumberSetting
        label="Scan every (minutes, 0 = off)"
        value={settings.ingest_interval_min}
        onSave={(v) => save({ ingest_interval_min: v }, "ingint")}
        saved={saved === "ingint"}
        hint="Only new or changed files are read, so frequent scans are cheap."
      />
      <NumberSetting
        label="Wait before reading a new file (seconds)"
        value={settings.ingest_settle_sec}
        onSave={(v) => save({ ingest_settle_sec: v }, "settle")}
        saved={saved === "settle"}
        hint="Files modified more recently than this are probably still being copied or downloaded, and are skipped until the next scan."
      />
    </div>
  );
}

function CalibreWriteSettings({ settings, save, saved }) {
  const [url, setUrl] = useState(settings.calibre_url);
  const [formats, setFormats] = useState(settings.replace_formats);
  const [test, setTest] = useState(null);
  useEffect(() => setUrl(settings.calibre_url), [settings.calibre_url]);
  useEffect(() => setFormats(settings.replace_formats), [settings.replace_formats]);
  const runTest = () => api.calibreTest().then(setTest).catch((e) => setTest({ ok: false, message: e.message }));
  return (
    <div className="card">
      <h2>Adding books to Calibre</h2>
      <label className="field">
        <span>How Tomearr writes to the library</span>
        <select value={settings.calibre_mode} onChange={(e) => save({ calibre_mode: e.target.value }, "mode")}>
          <option value="direct">Directly (no login)</option>
          <option value="server">Through a content server</option>
        </select>
        <span className="muted small-print">
          {settings.calibre_mode === "direct"
            ? "Calibre's calibredb updates metadata.db and names the folders, the same way Calibre-Web writes. The /calibre mount must be read-write. The Calibre desktop app shows new books after a restart (or Library → Check library)."
            : "Calibre only accepts changes from another machine or container from a logged-in Calibre user, so this mode needs a user set up in Calibre."}
        </span>
      </label>
      {settings.calibre_mode === "server" && (
        <label className="field">
          <span>Content server address</span>
          <span className="inline-field">
            <input value={url} placeholder="http://192.168.0.229:8221" onChange={(e) => setUrl(e.target.value)} spellCheck={false} />
            <button disabled={url === settings.calibre_url} onClick={() => save({ calibre_url: url }, "url")}>Save</button>
          </span>
        </label>
      )}
      <div className="actions">
        <button onClick={runTest}>Test writing to Calibre</button>
        {(saved === "mode" || saved === "url") && <span className="ok small-print">Saved</span>}
      </div>
      {test &&
        (test.ok ? (
          <div className="notice ok-notice">
            OK: {formatNumber(test.books)} books at {test.target} (calibre {test.version}).
          </div>
        ) : (
          <div className="notice error">{test.message}</div>
        ))}
      <label className="field">
        <span>Formats to replace once a book has an EPUB</span>
        <span className="inline-field">
          <input value={formats} onChange={(e) => setFormats(e.target.value)} spellCheck={false} className="wide" />
          <button disabled={formats === settings.replace_formats} onClick={() => save({ replace_formats: formats }, "fmts")}>Save</button>
          {saved === "fmts" && <span className="ok small-print">Saved</span>}
        </span>
        <span className="muted small-print">Removed from the Calibre book after its EPUB is confirmed (a copy goes to the trash first).</span>
      </label>
      <label className="check">
        <input type="checkbox" checked={settings.delete_duplicates} onChange={(e) => save({ delete_duplicates: e.target.checked }, "dups")} />
        Move ingest files that are identical to a file already in Calibre to the trash
      </label>
      <label className="check">
        <input type="checkbox" checked={settings.import_auto} onChange={(e) => save({ import_auto: e.target.checked }, "auto")} />
        Import automatically after each scan (confident matches and files you accepted; PDFs still need your OK)
      </label>
      <NumberSetting
        label="Keep the trash for (days)"
        value={settings.trash_days}
        onSave={(v) => save({ trash_days: v }, "trash")}
        saved={saved === "trash"}
        hint="Originals and replaced formats are kept this long so any import can be undone, then deleted for good."
      />
    </div>
  );
}
