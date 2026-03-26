"use client";

// Albums index page — grid of all albums with cover thumbnail, title, and asset count.
// Route: /albums

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { listAlbums, createAlbum, AlbumItem } from "@/lib/api";

export default function AlbumsPage() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [albums, setAlbums] = useState<AlbumItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // New album form
  const [showForm, setShowForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  useEffect(() => {
    if (!ready || !token) return;
    setLoading(true);
    listAlbums(token)
      .then(setAlbums)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load albums"))
      .finally(() => setLoading(false));
  }, [ready, token]);

  useEffect(() => {
    if (showForm) inputRef.current?.focus();
  }, [showForm]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const title = newTitle.trim();
    if (!title || !token) return;
    setCreating(true);
    setCreateError(null);
    try {
      const album = await createAlbum(token, title);
      setAlbums((prev) => [album, ...prev]);
      setNewTitle("");
      setShowForm(false);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create album");
    } finally {
      setCreating(false);
    }
  }

  if (!ready || !token) return null;

  return (
    <main className="min-h-screen bg-white px-4 py-6">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/" className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
              <path fillRule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
            </svg>
            Photos
          </Link>
          <h1 className="text-lg font-semibold text-gray-900">Albums</h1>
        </div>
        <button
          onClick={() => { setShowForm((v) => !v); setCreateError(null); }}
          className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white hover:bg-gray-700"
        >
          New album
        </button>
      </div>

      {/* New album form */}
      {showForm && (
        <form onSubmit={handleCreate} className="mb-6 flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="Album title"
            className="flex-1 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-400 focus:bg-white focus:outline-none"
          />
          <button
            type="submit"
            disabled={creating || !newTitle.trim()}
            className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white hover:bg-gray-700 disabled:opacity-40"
          >
            {creating ? "Creating…" : "Create"}
          </button>
          <button
            type="button"
            onClick={() => { setShowForm(false); setNewTitle(""); setCreateError(null); }}
            className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-600 hover:border-gray-400"
          >
            Cancel
          </button>
        </form>
      )}
      {createError && (
        <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700">{createError}</p>
      )}

      {/* Error / loading / empty states */}
      {error && (
        <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>
      )}
      {loading && (
        <p className="py-8 text-center text-sm text-gray-400">Loading…</p>
      )}
      {!loading && albums.length === 0 && !error && (
        <p className="mt-24 text-center text-gray-400">No albums yet. Create one to get started.</p>
      )}

      {/* Albums grid */}
      {!loading && albums.length > 0 && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {albums.map((album) => (
            <Link key={album.id} href={`/albums/${album.id}`} className="group block">
              {/* Cover */}
              <div className="aspect-square overflow-hidden rounded-lg bg-gray-100">
                {album.cover_thumbnail_url ? (
                  <img
                    src={album.cover_thumbnail_url}
                    alt={album.title}
                    className="h-full w-full object-cover transition-transform group-hover:scale-105"
                    onError={(e) => { e.currentTarget.style.display = "none"; }}
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-gray-300">
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-12 w-12">
                      <path fillRule="evenodd" d="M1.5 6a2.25 2.25 0 0 1 2.25-2.25h16.5A2.25 2.25 0 0 1 22.5 6v12a2.25 2.25 0 0 1-2.25 2.25H3.75A2.25 2.25 0 0 1 1.5 18V6ZM3 16.06V18c0 .414.336.75.75.75h16.5A.75.75 0 0 0 21 18v-1.94l-2.69-2.689a1.5 1.5 0 0 0-2.12 0l-.88.879.97.97a.75.75 0 1 1-1.06 1.06l-5.16-5.159a1.5 1.5 0 0 0-2.12 0L3 16.061Zm10.125-7.81a1.125 1.125 0 1 1 2.25 0 1.125 1.125 0 0 1-2.25 0Z" clipRule="evenodd" />
                    </svg>
                  </div>
                )}
              </div>
              {/* Title + count */}
              <div className="mt-2">
                <p className="truncate text-sm font-medium text-gray-900">{album.title}</p>
                <p className="text-xs text-gray-400">
                  {album.asset_count} {album.asset_count === 1 ? "photo" : "photos"}
                </p>
              </div>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}
