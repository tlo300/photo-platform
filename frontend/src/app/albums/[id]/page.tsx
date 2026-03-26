"use client";

// Album detail page — photo grid scoped to a single album with remove-from-album action.
// Route: /albums/[id]

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { listAlbums, getAlbumAssets, removeAssetFromAlbum, AlbumItem, AlbumAssetItem } from "@/lib/api";

export default function AlbumDetailPage() {
  const { token, ready } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const albumId = params.id;

  const [album, setAlbum] = useState<AlbumItem | null>(null);
  const [assets, setAssets] = useState<AlbumAssetItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  const load = useCallback(async () => {
    if (!token || !albumId) return;
    setLoading(true);
    setError(null);
    try {
      const [allAlbums, albumAssets] = await Promise.all([
        listAlbums(token),
        getAlbumAssets(token, albumId),
      ]);
      const found = allAlbums.find((a) => a.id === albumId) ?? null;
      setAlbum(found);
      setAssets(albumAssets);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load album");
    } finally {
      setLoading(false);
    }
  }, [token, albumId]);

  useEffect(() => {
    if (ready && token) load();
  }, [ready, token, load]);

  async function handleRemove(assetId: string) {
    if (!token) return;
    setRemoving(assetId);
    try {
      await removeAssetFromAlbum(token, albumId, assetId);
      setAssets((prev) => prev.filter((a) => a.id !== assetId));
      if (album) {
        setAlbum({ ...album, asset_count: Math.max(0, album.asset_count - 1) });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove photo");
    } finally {
      setRemoving(null);
    }
  }

  if (!ready || !token) return null;

  if (loading) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-gray-400">Loading…</p>
      </main>
    );
  }

  if (error || !album) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4">
        <p className="rounded bg-red-50 px-4 py-2 text-sm text-red-700">
          {error ?? "Album not found."}
        </p>
        <button onClick={() => router.back()} className="text-sm text-blue-600 underline">
          Go back
        </button>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-white px-4 py-6">
      {/* Header */}
      <div className="mb-6 flex items-center gap-4">
        <Link
          href="/albums"
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900"
        >
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path fillRule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
          </svg>
          Albums
        </Link>
        <div>
          <h1 className="text-lg font-semibold text-gray-900">{album.title}</h1>
          <p className="text-xs text-gray-400">
            {album.asset_count} {album.asset_count === 1 ? "photo" : "photos"}
          </p>
        </div>
      </div>

      {error && (
        <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>
      )}

      {assets.length === 0 && (
        <p className="mt-24 text-center text-gray-400">This album is empty.</p>
      )}

      {assets.length > 0 && (
        <div className="grid grid-cols-1 gap-0.5 sm:grid-cols-3 lg:grid-cols-5">
          {assets.map((asset) => (
            <div key={asset.id} className="group relative aspect-square overflow-hidden bg-gray-100">
              <Link href={`/assets/${asset.id}`} className="block h-full w-full">
                {asset.thumbnail_url ? (
                  <img
                    src={asset.thumbnail_url}
                    alt={asset.original_filename}
                    className="h-full w-full object-cover"
                    onError={(e) => { e.currentTarget.style.display = "none"; }}
                  />
                ) : (
                  <div className="h-full w-full animate-pulse bg-gray-200" />
                )}
              </Link>
              {/* Remove button — visible on hover */}
              <button
                onClick={() => handleRemove(asset.id)}
                disabled={removing === asset.id}
                className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white opacity-0 transition-opacity group-hover:opacity-100 hover:bg-black/80 disabled:cursor-not-allowed"
                title="Remove from album"
                aria-label="Remove from album"
              >
                {removing === asset.id ? (
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5 animate-spin">
                    <path fillRule="evenodd" d="M15.312 11.424a5.5 5.5 0 0 1-9.201 2.466l-.312-.311h2.433a.75.75 0 0 0 0-1.5H3.989a.75.75 0 0 0-.75.75v4.242a.75.75 0 0 0 1.5 0v-2.43l.31.31a7 7 0 0 0 11.712-3.138.75.75 0 0 0-1.449-.39Z" clipRule="evenodd" />
                  </svg>
                ) : (
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5">
                    <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
                  </svg>
                )}
              </button>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
