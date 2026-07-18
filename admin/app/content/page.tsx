"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type FileRef = { name: string; path: string };
type Folder = { name: string; files: FileRef[] };

export default function ContentPage() {
  const [folders, setFolders] = useState<Folder[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [msg, setMsg] = useState("");

  // Editor state: either an existing file (path set) or a new file (path built
  // from folder + filename). content holds the textarea body.
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  // New-file form
  const [newFolder, setNewFolder] = useState("");
  const [newName, setNewName] = useState("");

  function loadList() {
    setStatus("loading");
    fetch("/api/content")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((d) => {
        setFolders(d.folders);
        setNewFolder((f) => f || d.folders[0]?.name || "");
        setStatus("ready");
      })
      .catch((e) => { setStatus("error"); setMsg(e.message); });
  }
  useEffect(loadList, []);

  async function open(path: string) {
    setBusy(true); setMsg("");
    try {
      const r = await fetch(`/api/content?path=${encodeURIComponent(path)}`);
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `error ${r.status}`);
      setOpenPath(path); setContent(j.content); setEditing(true);
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  }

  function newFile() {
    const name = newName.trim().replace(/\.md$/i, "");
    if (!newFolder || !name) { setMsg("pick a folder and a filename"); return; }
    setOpenPath(`inputs/content/${newFolder}/${name}.md`);
    setContent(""); setEditing(true); setMsg("");
  }

  async function save() {
    if (!openPath) return;
    setBusy(true); setMsg("");
    try {
      const r = await fetch("/api/content", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: openPath, content }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `error ${r.status}`);
      setMsg(j.deploy?.triggered ? "Saved — live in a few minutes." : "Saved to git.");
      setEditing(false); setOpenPath(null); loadList();
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  }

  async function del(path: string) {
    if (!confirm(`Delete ${path.split("/").pop()}? This removes it from the agent's taste profile.`)) return;
    setBusy(true); setMsg("");
    try {
      const r = await fetch(`/api/content?path=${encodeURIComponent(path)}`, { method: "DELETE" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `error ${r.status}`);
      setMsg("Deleted.");
      if (openPath === path) { setEditing(false); setOpenPath(null); }
      loadList();
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  }

  if (status === "loading") return <Layout><p className="text-sm text-gray-500">Loading content corpus…</p></Layout>;
  if (status === "error") return <Layout><div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div></Layout>;
  if (!folders) return null;

  if (editing) {
    const isNew = !folders.some((f) => f.files.some((x) => x.path === openPath));
    return (
      <Layout>
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-sm font-medium">{openPath}</div>
            <div className="text-xs text-gray-500">{isNew ? "New file" : "Editing"}</div>
          </div>
          <button onClick={() => { setEditing(false); setOpenPath(null); }}
            className="text-sm text-gray-600 hover:text-gray-900">Cancel</button>
        </div>
        <textarea value={content} onChange={(e) => setContent(e.target.value)}
          className="w-full h-[60vh] border rounded p-3 font-mono text-xs" />
        <div className="mt-4 flex items-center gap-4">
          <button onClick={save} disabled={busy}
            className="bg-black text-white rounded px-6 py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-50">
            {busy ? "Saving…" : "Save"}
          </button>
          {msg && <span className="text-sm text-green-700">{msg}</span>}
        </div>
      </Layout>
    );
  }

  return (
    <Layout>
      <p className="text-sm text-gray-500 mb-4">
        The firm’s own published content — the “taste profile” the agent scores
        story relevance against. Every <code>.md</code> file here gets embedded.
        Add, edit, or remove pieces; saving commits to the repo.
      </p>

      <div className="bg-white border rounded p-4 mb-6">
        <div className="text-xs font-medium uppercase text-gray-600 mb-2">Add a piece</div>
        <div className="flex flex-wrap items-center gap-2">
          <select value={newFolder} onChange={(e) => setNewFolder(e.target.value)}
            className="border rounded px-2 py-1.5 text-sm">
            {folders.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
          </select>
          <input value={newName} onChange={(e) => setNewName(e.target.value)}
            placeholder="filename (without .md)"
            className="border rounded px-2 py-1.5 text-sm w-72" />
          <span className="text-xs text-gray-400">.md</span>
          <button onClick={newFile} className="text-sm border rounded px-3 py-1.5 hover:bg-gray-50">
            + New
          </button>
        </div>
      </div>

      {msg && <div className="text-sm text-green-700 mb-3">{msg}</div>}

      {folders.map((f) => (
        <div key={f.name} className="mb-6">
          <h3 className="text-sm font-semibold mb-2">
            {f.name} <span className="text-gray-400 font-normal">({f.files.length})</span>
          </h3>
          <div className="bg-white border rounded divide-y">
            {f.files.map((file) => (
              <div key={file.path} className="flex items-center justify-between px-3 py-2 text-sm">
                <span className="truncate mr-4">{file.name}</span>
                <span className="flex gap-2 shrink-0">
                  <button onClick={() => open(file.path)} disabled={busy}
                    className="border rounded px-2 py-1 text-xs hover:bg-gray-50">Edit</button>
                  <button onClick={() => del(file.path)} disabled={busy}
                    className="border rounded px-2 py-1 text-xs hover:bg-red-50 hover:text-red-700">Delete</button>
                </span>
              </div>
            ))}
            {f.files.length === 0 && <div className="px-3 py-2 text-xs text-gray-400">empty</div>}
          </div>
        </div>
      ))}
    </Layout>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">← Back</Link>
          <h1 className="text-lg font-semibold">Content corpus</h1>
          <div className="w-12" />
        </div>
      </header>
      <main className="max-w-4xl mx-auto px-6 py-8 pb-32">{children}</main>
    </div>
  );
}
