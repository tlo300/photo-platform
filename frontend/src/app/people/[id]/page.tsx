"use client";

// Person detail page — justified grid of all photos featuring a specific person.
// Route: /people/[id]

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getAssets, listPeople, AssetItem, PersonItem } from "@/lib/api";
import { MediaCard } from "@/components/MediaCard";

// ─── Layout constants ──────────────────────────────────────────────────────────

const TARGET_ROW_HEIGHT = 200; // px

// ─── Justified-layout helpers ──────────────────────────────────────────────────

interface RenderedDims {
  width: number;
  height: number;
}

function justifyRow(
  assets: AssetItem[],
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
  assets: AssetItem[];
  isPartial: boolean;
}

function buildRows(
  assets: AssetItem[],
  containerWidth: number,
  targetHeight: number
): AssetRow[] {
  if (containerWidth === 0) return [];
  const rows: AssetRow[] = [];
  let current: AssetItem[] = [];
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
  assets: AssetItem[];
}

function groupByDay(items: AssetItem[]): DayGroup[] {
  const map = new Map<string, AssetItem[]>();
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
    else if (localities.length > 2)
      locationSummary = `${localities[0]} & ${localities.length - 1} more`;
    return { date, label, locationSummary, assets };
  });
}

// ─── Sub-components ────────────────────────────────────────────────────────────

function JustifiedRow({
  row,
  containerWidth,
  token,
}: {
  row: AssetRow;
  containerWidth: number;
  token: string;
}) {
  const dims = justifyRow(row.assets, containerWidth, TARGET_ROW_HEIGHT, row.isPartial);
  return (
    <div className="flex gap-0.5">
      {row.assets.map((asset, i) => (
        <div
          key={asset.id}
          className="flex-shrink-0"
          style={{ width: dims[i].width, height: dims[i].height }}
        >
          <MediaCard
            asset={asset}
            width={dims[i].width}
            height={dims[i].height}
            token={token}
            onClick={() => {}}
          />
        </div>
      ))}
    </div>
  );
}

function DaySection({
  group,
  containerWidth,
  token,
}: {
  group: DayGroup;
  containerWidth: number;
  token: string;
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
          <JustifiedRow key={i} row={row} containerWidth={containerWidth} token={token} />
        ))}
      </div>
    </section>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function PersonPage() {
  const { token, ready } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const personId = params.id;

  const [person, setPerson] = useState<PersonItem | null>(null);
  const [items, setItems] = useState<AssetItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);

  const gridRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  // Measure grid container width for justified layout.
  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      setContainerWidth(Math.floor(entries[0]?.contentRect.width ?? 0));
    });
    ro.observe(el);
    setContainerWidth(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);

  // Initial load: person info + first page of photos.
  useEffect(() => {
    if (!ready || !token) return;
    setLoading(true);
    Promise.all([
      listPeople(token),
      getAssets(token, undefined, 50, undefined, undefined, personId),
    ])
      .then(([people, page]) => {
        setPerson(people.find((p) => p.id === personId) ?? null);
        setItems(page.items);
        setNextCursor(page.next_cursor);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [ready, token, personId]);

  // Load next page.
  const loadMore = useCallback(async () => {
    if (!token || !nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getAssets(token, nextCursor, 50, undefined, undefined, personId);
      setItems((prev) => [...prev, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch {
      // silently ignore — user can scroll again to retry
    } finally {
      setLoadingMore(false);
    }
  }, [token, nextCursor, loadingMore, personId]);

  // Infinite scroll sentinel.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) loadMore();
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, [loadMore]);

  if (!ready || !token) return null;

  const dayGroups = groupByDay(items);
  const photoCount = person?.photo_count ?? items.length;

  return (
    <main className="min-h-screen bg-white px-4 py-6 dark:bg-gray-900">
      {/* Header */}
      <div className="mb-6 flex items-center gap-4">
        <Link
          href="/people"
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
        >
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path fillRule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
          </svg>
          People
        </Link>
        <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          {person?.name ?? "Person"}
        </h1>
        {!loading && (
          <span className="text-sm text-gray-400 dark:text-gray-500">
            {photoCount} {photoCount === 1 ? "photo" : "photos"}
          </span>
        )}
      </div>

      {/* States */}
      {error && (
        <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
          {error}
        </p>
      )}
      {loading && (
        <p className="py-8 text-center text-sm text-gray-400 dark:text-gray-500">Loading…</p>
      )}
      {!loading && items.length === 0 && !error && (
        <p className="mt-24 text-center text-gray-400 dark:text-gray-500">No photos found.</p>
      )}

      {/* Justified grid */}
      <div ref={gridRef}>
        {!loading &&
          dayGroups.map((group) => (
            <DaySection
              key={group.date}
              group={group}
              containerWidth={containerWidth}
              token={token}
            />
          ))}
      </div>

      {/* Infinite scroll sentinel */}
      {nextCursor && <div ref={sentinelRef} className="h-16" />}
      {loadingMore && (
        <p className="py-4 text-center text-sm text-gray-400 dark:text-gray-500">Loading more…</p>
      )}
    </main>
  );
}
