"use client";

// Album detail page — justified grid grouped by day, same look as the photo feed.
// Route: /albums/[id]

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import {
  listAlbums,
  getAlbumAssets,
  removeAssetFromAlbum,
  updateAlbumHidden,
  updateAlbumCover,
  AlbumItem,
  AlbumAssetItem,
  AssetItem,
} from "@/lib/api";
import { MediaCard } from "@/components/MediaCard";

// ─── Layout constants ──────────────────────────────────────────────────────────

const TARGET_ROW_HEIGHT = 200; // px

// ─── Justified-layout helpers (mirrors page.tsx) ───────────────────────────────

interface RenderedDims {
  width: number;
  height: number;
}

function justifyRow(
  assets: AlbumAssetItem[],
  containerWidth: number,
  targetRowHeight: number,
  isPartialRow: boolean
): RenderedDims[] {
  if (assets.length === 0) return [];
  const aspectRatios = assets.map((a) =>
    a.width && a.height ? a.width / a.height : 1
  );
  const totalAspect = aspectRatios.reduce((s, ar) => s + ar, 0);
  const rowHeight = isPartialRow ? targetRowHeight : containerWidth / totalAspect;
  return aspectRatios.map((ar) => ({
    width: Math.floor(ar * rowHeight),
    height: Math.floor(rowHeight),
  }));
}

interface AssetRow {
  assets: AlbumAssetItem[];
  isPartial: boolean;
}

function buildRows(
  assets: AlbumAssetItem[],
  containerWidth: number,
  targetHeight: number
): AssetRow[] {
  if (containerWidth === 0) return [];
  const rows: AssetRow[] = [];
  let current: AlbumAssetItem[] = [];
  let aspectSum = 0;

  for (const asset of assets) {
    const ar = asset.width && asset.height ? asset.width / asset.height : 1;
    current.push(asset);
    aspectSum += ar;
    if (containerWidth / aspectSum <= targetHeight) {
      rows.push({ assets: current, isPartial: false });
      current = [];
      aspectSum = 0;
    }
  }
  if (current.length > 0) rows.push({ assets: current, isPartial: true });
  return rows;
}

// ─── Day grouping ──────────────────────────────────────────────────────────────

interface DayGroup {
  date: string;
  label: string;
  locationSummary: string;
  assets: AlbumAssetItem[];
}

function groupByDay(items: AlbumAssetItem[]): DayGroup[] {
  const map = new Map<string, AlbumAssetItem[]>();
  for (const item of items) {
    const date = item.captured_at ? item.captured_at.slice(0, 10) : "unknown";
    if (!map.has(date)) map.set(date, []);
    map.get(date)!.push(item);
  }
  return Array.from(map.entries()).map(([date, assets]) => {
    const label =
      date === "unknown"
        ? "Unknown date"
        : new Date(date + "T12:00:00").toLocaleDateString("en-GB", {
            weekday: "short",
            day: "numeric",
            month: "short",
            year: "numeric",
          });
    const localities = Array.from(
      new Set(assets.map((a) => a.locality).filter((l): l is string => !!l))
    );
    let locationSummary = "";
    if (localities.length === 1) locationSummary = localities[0];
    else if (localities.length === 2) locationSummary = `${localities[0]} & ${localities[1]}`;
    else if (localities.length > 2) locationSummary = `${localities[0]} & ${localities.length - 1} more`;
    return { date, label, locationSummary, assets };
  });
}

// ─── Sub-components ────────────────────────────────────────────────────────────

function JustifiedRow({
  row,
  containerWidth,
  token,
  removing,
  settingCover,
  coverAssetId,
  onRemove,
  onSetCover,
}: {
  row: AssetRow;
  containerWidth: number;
  token: string;
  removing: string | null;
  settingCover: boolean;
  coverAssetId: string | null;
  onRemove: (id: string) => void;
  onSetCover: (id: string) => void;
}) {
  const dims = justifyRow(row.assets, containerWidth, TARGET_ROW_HEIGHT, row.isPartial);
  return (
    <div className="flex gap-0.5">
      {row.assets.map((asset, i) => (
        <div
          key={asset.id}
          className="group relative flex-shrink-0"
          style={{ width: dims[i].width, height: dims[i].height }}
        >
          <MediaCard
            asset={asset as unknown as AssetItem}
            width={dims[i].width}
            height={dims[i].height}
            token={token}
            onClick={() => {}}
          />
          {/* Cover indicator */}
          {asset.id === coverAssetId && (
            <div className="pointer-events-none absolute left-1.5 top-1.5 flex h-6 w-6 items-center justify-center rounded-full bg-black/60 text-white">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5">
                <path fillRule="evenodd" d="M10.868 2.884c-.321-.772-1.415-.772-1.736 0l-1.83 4.401-4.753.381c-.833.067-1.171 1.107-.536 1.651l3.62 3.102-1.106 4.637c-.194.813.691 1.456 1.405 1.02L10 15.591l4.069 2.485c.713.436 1.598-.207 1.404-1.02l-1.106-4.637 3.62-3.102c.635-.544.297-1.584-.536-1.65l-4.752-.382-1.83-4.402Z" clipRule="evenodd" />
              </svg>
            </div>
          )}
          {/* Set as cover button — visible on hover, hidden when already cover */}
          {asset.id !== coverAssetId && (
            <button
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onSetCover(asset.id);
              }}
              disabled={settingCover}
              className="absolute left-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white opacity-0 transition-opacity group-hover:opacity-100 hover:bg-black/80 disabled:cursor-not-allowed"
              title="Set as album cover"
              aria-label="Set as album cover"
            >
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5">
                <path fillRule="evenodd" d="M10.868 2.884c-.321-.772-1.415-.772-1.736 0l-1.83 4.401-4.753.381c-.833.067-1.171 1.107-.536 1.651l3.62 3.102-1.106 4.637c-.194.813.691 1.456 1.405 1.02L10 15.591l4.069 2.485c.713.436 1.598-.207 1.404-1.02l-1.106-4.637 3.62-3.102c.635-.544.297-1.584-.536-1.65l-4.752-.382-1.83-4.402Z" clipRule="evenodd" />
              </svg>
            </button>
          )}
          {/* Remove from album button */}
          <button
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onRemove(asset.id);
            }}
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
  );
}

function DaySection({
  group,
  containerWidth,
  token,
  removing,
  settingCover,
  coverAssetId,
  onRemove,
  onSetCover,
}: {
  group: DayGroup;
  containerWidth: number;
  token: string;
  removing: string | null;
  settingCover: boolean;
  coverAssetId: string | null;
  onRemove: (id: string) => void;
  onSetCover: (id: string) => void;
}) {
  const rows = buildRows(group.assets, containerWidth, TARGET_ROW_HEIGHT);
  return (
    <section className="mb-6">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-sm font-semibold text-gray-800 dark:text-gray-200">{group.label}</h2>
        {group.locationSummary && (
          <span className="text-xs text-gray-400 dark:text-gray-500">{group.locationSummary}</span>
        )}
      </div>
      <div className="flex flex-col gap-0.5">
        {rows.map((row, i) => (
          <JustifiedRow
            key={i}
            row={row}
            containerWidth={containerWidth}
            token={token}
            removing={removing}
            settingCover={settingCover}
            coverAssetId={coverAssetId}
            onRemove={onRemove}
            onSetCover={onSetCover}
          />
        ))}
      </div>
    </section>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────────

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
  const [togglingHidden, setTogglingHidden] = useState(false);
  const [settingCover, setSettingCover] = useState(false);
  const [containerWidth, setContainerWidth] = useState(0);

  const gridRef = useRef<HTMLDivElement>(null);

  // Measure grid width for justified layout.
  // gridRef div is always in the DOM so this runs once on mount with a valid ref.
  useLayoutEffect(() => {
    if (!gridRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      setContainerWidth(Math.floor(w));
    });
    ro.observe(gridRef.current);
    // Also read immediately so the first render after loading has a non-zero width.
    setContainerWidth(Math.floor(gridRef.current.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);

  useLayoutEffect(() => {
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

  useLayoutEffect(() => {
    if (ready && token) load();
  }, [ready, token, load]);

  async function handleToggleHidden() {
    if (!token || !album || togglingHidden) return;
    setTogglingHidden(true);
    try {
      const updated = await updateAlbumHidden(token, albumId, !album.is_hidden);
      setAlbum(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update album");
    } finally {
      setTogglingHidden(false);
    }
  }

  async function handleSetCover(assetId: string) {
    if (!token || !album || settingCover) return;
    setSettingCover(true);
    try {
      const updated = await updateAlbumCover(token, albumId, assetId);
      setAlbum(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to set cover photo");
    } finally {
      setSettingCover(false);
    }
  }

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

  const dayGroups = groupByDay(assets);

  return (
    <main className="min-h-screen bg-white px-4 py-6 dark:bg-gray-900">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <Link
            href="/albums"
            className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
              <path fillRule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
            </svg>
            Albums
          </Link>
          {album && (
            <div>
              <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{album.title}</h1>
              <p className="text-xs text-gray-400 dark:text-gray-500">
                {album.asset_count} {album.asset_count === 1 ? "photo" : "photos"}
              </p>
            </div>
          )}
        </div>
        {album && (
          <button
            onClick={handleToggleHidden}
            disabled={togglingHidden}
            className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-40 ${
              album.is_hidden
                ? "border-gray-300 bg-gray-100 text-gray-500 hover:border-gray-400 hover:text-gray-700 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-400 dark:hover:border-gray-500"
                : "border-gray-200 text-gray-500 hover:border-gray-400 hover:text-gray-700 dark:border-gray-700 dark:text-gray-400 dark:hover:border-gray-500"
            }`}
            title={album.is_hidden ? "Show in feed" : "Hide from feed"}
          >
            {album.is_hidden ? (
              <>
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
                  <path fillRule="evenodd" d="M3.28 2.22a.75.75 0 0 0-1.06 1.06l14.5 14.5a.75.75 0 1 0 1.06-1.06l-1.745-1.745a10.029 10.029 0 0 0 3.3-4.38 1.651 1.651 0 0 0 0-1.185A10.004 10.004 0 0 0 9.999 3a9.956 9.956 0 0 0-4.744 1.194L3.28 2.22ZM7.752 6.69l1.092 1.092a2.5 2.5 0 0 1 3.374 3.373l1.091 1.092a4 4 0 0 0-5.557-5.557Z" clipRule="evenodd" />
                  <path d="M10.748 13.93l2.523 2.523a9.987 9.987 0 0 1-3.27.547c-4.258 0-7.894-2.66-9.337-6.41a1.651 1.651 0 0 1 0-1.186A10.007 10.007 0 0 1 2.839 6.02L6.07 9.252a4 4 0 0 0 4.678 4.678Z" />
                </svg>
                Hidden from feed
              </>
            ) : (
              <>
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
                  <path d="M10 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" />
                  <path fillRule="evenodd" d="M.664 10.59a1.651 1.651 0 0 1 0-1.186A10.004 10.004 0 0 1 10 3c4.257 0 7.893 2.66 9.336 6.41.147.381.146.804 0 1.186A10.004 10.004 0 0 1 10 17c-4.257 0-7.893-2.66-9.336-6.41Z" clipRule="evenodd" />
                </svg>
                Visible in feed
              </>
            )}
          </button>
        )}
      </div>

      {/* Loading / error / empty states — grid div always stays in DOM for ResizeObserver */}
      {loading && (
        <p className="mt-24 text-center text-sm text-gray-400 dark:text-gray-500">Loading…</p>
      )}
      {!loading && error && (
        <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">{error}</p>
      )}
      {!loading && !error && assets.length === 0 && (
        <p className="mt-24 text-center text-gray-400 dark:text-gray-500">This album is empty.</p>
      )}

      <div ref={gridRef}>
        {dayGroups.map((group) => (
          <DaySection
            key={group.date}
            group={group}
            containerWidth={containerWidth}
            token={token}
            removing={removing}
            settingCover={settingCover}
            coverAssetId={album?.cover_asset_id ?? null}
            onRemove={handleRemove}
            onSetCover={handleSetCover}
          />
        ))}
      </div>
    </main>
  );
}
