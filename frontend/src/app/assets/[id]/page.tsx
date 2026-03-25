"use client";

// Asset detail page — shows full-resolution photo or video, metadata panel,
// optional GPS map, and tags. Route: /assets/[id]

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getAsset, AssetDetail } from "@/lib/api";

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function AssetDetailPage() {
  const { token, ready } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const id = params.id;

  const [asset, setAsset] = useState<AssetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  useEffect(() => {
    if (!ready || !token || !id) return;
    setLoading(true);
    getAsset(token, id)
      .then(setAsset)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load asset"))
      .finally(() => setLoading(false));
  }, [ready, token, id]);

  if (!ready || !token) return null;

  if (loading) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-gray-400">Loading…</p>
      </main>
    );
  }

  if (error || !asset) {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4">
        <p className="rounded bg-red-50 px-4 py-2 text-sm text-red-700">
          {error ?? "Asset not found."}
        </p>
        <button onClick={() => router.back()} className="text-sm text-blue-600 underline">
          Go back
        </button>
      </main>
    );
  }

  const isVideo = asset.mime_type.startsWith("video/");
  const capturedAt = asset.captured_at
    ? new Date(asset.captured_at).toLocaleString("en-US", {
        year: "numeric",
        month: "long",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <main className="min-h-screen bg-white">
      {/* Top bar */}
      <div className="sticky top-0 z-10 flex items-center gap-3 border-b bg-white px-4 py-3">
        <button
          onClick={() => router.back()}
          className="flex items-center gap-1 text-sm text-gray-600 hover:text-gray-900"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            className="h-4 w-4"
          >
            <path
              fillRule="evenodd"
              d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z"
              clipRule="evenodd"
            />
          </svg>
          Back
        </button>
        <span className="truncate text-sm text-gray-500">{asset.original_filename}</span>
      </div>

      <div className="mx-auto max-w-5xl px-4 py-6 lg:flex lg:gap-8">
        {/* Media */}
        <div className="flex flex-1 items-start justify-center">
          {isVideo ? (
            <video
              src={asset.full_url}
              controls
              className="max-h-[70vh] w-full rounded object-contain"
            />
          ) : (
            <img
              src={asset.full_url}
              alt={asset.original_filename}
              className="max-h-[70vh] w-full rounded object-contain"
            />
          )}
        </div>

        {/* Sidebar */}
        <aside className="mt-6 w-full shrink-0 space-y-6 lg:mt-0 lg:w-72">

          {/* Date */}
          {capturedAt && (
            <section>
              <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-400">Date</h2>
              <p className="text-sm text-gray-700">{capturedAt}</p>
            </section>
          )}

          {/* Description */}
          {asset.description && (
            <section>
              <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-gray-400">Description</h2>
              <p className="text-sm text-gray-700">{asset.description}</p>
            </section>
          )}

          {/* File info */}
          <section>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">File</h2>
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-gray-400">Name</dt>
                <dd className="max-w-[60%] truncate text-right text-gray-700">{asset.original_filename}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-400">Type</dt>
                <dd className="text-gray-700">{asset.mime_type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-400">Size</dt>
                <dd className="text-gray-700">{formatFileSize(asset.file_size_bytes)}</dd>
              </div>
              {asset.metadata?.width_px && asset.metadata.height_px && (
                <div className="flex justify-between">
                  <dt className="text-gray-400">Dimensions</dt>
                  <dd className="text-gray-700">{asset.metadata.width_px} × {asset.metadata.height_px}</dd>
                </div>
              )}
              {asset.metadata?.duration_seconds && (
                <div className="flex justify-between">
                  <dt className="text-gray-400">Duration</dt>
                  <dd className="text-gray-700">{formatDuration(asset.metadata.duration_seconds)}</dd>
                </div>
              )}
            </dl>
          </section>

          {/* Camera */}
          {asset.metadata && (asset.metadata.make || asset.metadata.model) && (
            <section>
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Camera</h2>
              <dl className="space-y-1 text-sm">
                {asset.metadata.make && (
                  <div className="flex justify-between">
                    <dt className="text-gray-400">Make</dt>
                    <dd className="text-gray-700">{asset.metadata.make}</dd>
                  </div>
                )}
                {asset.metadata.model && (
                  <div className="flex justify-between">
                    <dt className="text-gray-400">Model</dt>
                    <dd className="text-gray-700">{asset.metadata.model}</dd>
                  </div>
                )}
              </dl>
            </section>
          )}

          {/* Tags */}
          {asset.tags.length > 0 && (
            <section>
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Tags</h2>
              <div className="flex flex-wrap gap-2">
                {asset.tags.map((tag) => (
                  <span
                    key={tag.name}
                    className="flex items-center gap-1 rounded-full bg-gray-100 px-3 py-1 text-xs text-gray-700"
                  >
                    {tag.name}
                    {tag.source && (
                      <span className="text-gray-400">· {tag.source.replace("google_", "")}</span>
                    )}
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Location */}
          {asset.location && (
            <section>
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">Location</h2>
              {(asset.location.display_name || asset.location.country) && (
                <p className="mb-2 text-sm text-gray-700">
                  {asset.location.display_name ?? asset.location.country}
                </p>
              )}
              <div className="overflow-hidden rounded border">
                <iframe
                  title="Map"
                  width="100%"
                  height="200"
                  src={`https://www.openstreetmap.org/export/embed.html?bbox=${asset.location.longitude - 0.02},${asset.location.latitude - 0.015},${asset.location.longitude + 0.02},${asset.location.latitude + 0.015}&layer=mapnik&marker=${asset.location.latitude},${asset.location.longitude}`}
                  className="border-0"
                />
              </div>
              <dl className="mt-2 space-y-1 text-xs text-gray-400">
                <div className="flex justify-between">
                  <dt>Coordinates</dt>
                  <dd>{asset.location.latitude.toFixed(5)}, {asset.location.longitude.toFixed(5)}</dd>
                </div>
                {asset.location.altitude_metres && (
                  <div className="flex justify-between">
                    <dt>Altitude</dt>
                    <dd>{Math.round(asset.location.altitude_metres)} m</dd>
                  </div>
                )}
              </dl>
            </section>
          )}
        </aside>
      </div>
    </main>
  );
}
