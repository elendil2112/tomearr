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

      <ProfileSettings settings={settings} save={save} saved={saved} />

      <DownloadSettings settings={settings} save={save} saved={saved} />

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
        <input type="checkbox" checked={settings.fetch_covers} onChange={(e) => save({ fetch_covers: e.target.checked }, "covers")} />
        Give imported books the Open Library cover when the file has none
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
        hint="Originals and replaced formats are kept this long so any import can be undone, then deleted for good. 0 deletes them as soon as Calibre confirms the import (those imports can't be undone)."
      />
    </div>
  );
}

function ProfileSettings({ settings, save, saved }) {
  const [langs, setLangs] = useState(settings.profile_languages);
  useEffect(() => setLangs(settings.profile_languages), [settings.profile_languages]);
  return (
    <div className="card">
      <h2>Which books count as wanted</h2>
      <p className="muted small-print">
        Open Library lists everything under an author: translations, study guides, box sets, one-off reprints. For
        monitored authors, only books that pass these rules are wanted. Books you mark Want yourself always are.
      </p>
      <label className="field">
        <span>Languages (MARC codes, comma-separated; blank = any)</span>
        <span className="inline-field">
          <input value={langs} onChange={(e) => setLangs(e.target.value)} spellCheck={false} placeholder="eng" className="wide" />
          <button disabled={langs === settings.profile_languages} onClick={() => save({ profile_languages: langs }, "plangs")}>
            Save
          </button>
          {saved === "plangs" && <span className="ok small-print">Saved</span>}
        </span>
        <span className="muted small-print">
          {LANGUAGES.slice(0, 8)
            .map(([c, n]) => `${c} = ${n}`)
            .join(", ")}
          …
        </span>
      </label>
      <NumberSetting
        label="At least this many editions"
        value={settings.profile_min_editions}
        onSave={(v) => save({ profile_min_editions: v }, "peds")}
        saved={saved === "peds"}
        hint="Single-edition works are often duplicates or obscure printings. Books from the last two years only need one, since new releases haven’t had time to collect more."
      />
      <label className="check">
        <input
          type="checkbox"
          checked={settings.profile_exclude_collections}
          onChange={(e) => save({ profile_exclude_collections: e.target.checked }, "pcoll")}
        />
        Skip collections, box sets, omnibuses and study guides
      </label>
    </div>
  );
}

function TextSetting({ label, value, onSave, saved, placeholder, hint, secret = false, isSet = false }) {
  const [v, setV] = useState(secret ? "" : value || "");
  useEffect(() => setV(secret ? "" : value || ""), [value, secret, isSet]);
  return (
    <label className="field">
      <span>{label}</span>
      <span className="inline-field">
        <input
          type={secret ? "password" : "text"}
          className="wide"
          value={v}
          placeholder={secret ? (isSet ? "•••••••• (saved; type to replace)" : "not set") : placeholder}
          onChange={(e) => setV(e.target.value)}
          spellCheck={false}
          autoComplete="off"
        />
        <button disabled={secret ? !v : v === (value || "")} onClick={() => onSave(v)}>
          Save
        </button>
        {saved && <span className="ok small-print">Saved</span>}
      </span>
      {hint && <span className="muted small-print">{hint}</span>}
    </label>
  );
}

function ClientTest({ client, label }) {
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    setRes(null);
    try {
      setRes(await api.downloadsTest(client));
    } catch (e) {
      setRes({ ok: false, message: e.message });
    } finally {
      setBusy(false);
    }
  };
  let msg = null;
  if (res?.ok) {
    if (client === "prowlarr") {
      const t = res.indexers.filter((i) => i.protocol === "torrent").length;
      const u = res.indexers.filter((i) => i.protocol === "usenet").length;
      msg = `OK: Prowlarr ${res.version}, ${t} torrent and ${u} usenet indexers enabled.`;
    } else if (client === "deluge") {
      msg = `OK: connected to Deluge${res.version ? ` ${res.version}` : ""}${res.label_plugin ? "" : ". The Label plugin is off, so labels won't be set."}`;
    } else {
      msg = `OK: SABnzbd ${res.version}. Categories: ${(res.categories || []).join(", ")}.`;
    }
  }
  return (
    <>
      <div className="actions">
        <button onClick={run} disabled={busy}>
          {busy ? "Testing…" : `Test ${label}`}
        </button>
      </div>
      {res && (res.ok ? <div className="notice ok-notice">{msg}</div> : <div className="notice error">{res.message}</div>)}
    </>
  );
}

function DownloadSettings({ settings, save, saved }) {
  const t = (key, label, extra = {}) => (
    <TextSetting
      label={label}
      value={settings[key]}
      isSet={settings[`${key}_set`]}
      onSave={(v) => save({ [key]: v }, key)}
      saved={saved === key}
      {...extra}
    />
  );
  return (
    <div className="card">
      <h2>Indexers and download clients</h2>
      <p className="muted small-print">
        Tomearr searches your indexers through Prowlarr, sends torrents to Deluge and NZBs to SABnzbd, then follows each
        download. Finished downloads need to land in the ingest folder ({settings.ingest_path} inside Tomearr), where
        they’re matched and imported like everything else.
      </p>

      <div className="client-block">
        <h3>Prowlarr</h3>
        {t("prowlarr_url", "Address", { placeholder: "http://192.168.0.229:9696" })}
        {t("prowlarr_api_key", "API key", { secret: true, hint: "Prowlarr → Settings → General → API Key." })}
        <ClientTest client="prowlarr" label="Prowlarr" />
      </div>

      <div className="client-block">
        <h3>Deluge (torrents)</h3>
        {t("deluge_url", "Web UI address", { placeholder: "http://192.168.0.229:8112" })}
        {t("deluge_password", "Web UI password", { secret: true })}
        {t("deluge_label", "Label", {
          placeholder: "ebooks",
          hint: "Set on every torrent Tomearr adds (needs Deluge's Label plugin). Give the label a “move completed to” folder that is your ingest folder.",
        })}
        {t("deluge_path", "Download folder (optional, as Deluge sees it)", {
          placeholder: "/data/download/completed/ebooks",
          hint: "Leave blank to use the label’s or Deluge’s own folder.",
        })}
        <ClientTest client="deluge" label="Deluge" />
      </div>

      <label className="check">
        <input type="checkbox" checked={settings.keep_seeding} onChange={(e) => save({ keep_seeding: e.target.checked }, "seed")} />
        Leave files from torrents that are still in Deluge in the ingest folder after importing them (so they keep
        seeding); they’re removed once the torrent is gone from Deluge
      </label>

      <div className="client-block">
        <h3>SABnzbd (usenet)</h3>
        {t("sab_url", "Address", { placeholder: "http://192.168.0.229:8080" })}
        {t("sab_api_key", "API key", { secret: true, hint: "SABnzbd → Config → General → API Key." })}
        {t("sab_category", "Category", {
          placeholder: "books",
          hint: "Give this category a folder that is your ingest folder (SABnzbd → Config → Categories).",
        })}
        <ClientTest client="sabnzbd" label="SABnzbd" />
      </div>

      <div className="client-block">
        <h3>Release rules</h3>
        <NumberSetting
          label="Largest release to grab (MB, 0 = no limit)"
          value={settings.release_max_size_mb}
          onSave={(v) => save({ release_max_size_mb: v }, "maxmb")}
          saved={saved === "maxmb"}
          hint="Ebooks are rarely over 50 MB; bigger releases are usually audiobooks or collections."
        />
        <NumberSetting
          label="Fewest seeders for a torrent"
          value={settings.release_min_seeders}
          onSave={(v) => save({ release_min_seeders: v }, "seeders")}
          saved={saved === "seeders"}
        />
      </div>
    </div>
  );
}
