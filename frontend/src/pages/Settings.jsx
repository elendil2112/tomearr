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
          that is <code>/calibre</code>. Tomearr copies <code>metadata.db</code> and reads the copy, so it never locks or
          changes your library, even while Calibre is running.
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
        Where new and unsorted ebooks land, as the container sees it (default <code>/ingest</code>). For now Tomearr only
        reads and reports on this folder; nothing is moved until the import step is built.
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
