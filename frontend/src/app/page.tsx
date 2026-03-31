"use client";

/**
 * Home page — Google Photos-style justified grid grouped by day with a
 * right-side timeline scrubber.  The photo area scrolls inside a flex
 * container so no browser-level scrollbar is shown.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getAssets, getAssetYears, searchAssets, AssetItem } from "@/lib/api";
import { MediaCard } from "@/components/MediaCard";

// ─── Constants ────────────────────────────────────────────────────────────────

const SCROLL_KEY = "home-scroll-top";
const TARGET_ROW_HEIGHT = 200; // px

// ─── Justified-layout helpers ─────────────────────────────────────────────────

interface RenderedDims {
  width: number;
  height: number;
}

/**
 * Given a set of assets and a container width, compute the rendered
 * width/height for each asset so that the row fills `containerWidth` exactly
 * at a uniform row height derived from the assets' aspect ratios.
 *
 * For a partial (last) row the assets are left-aligned at `targetRowHeight`.
 */
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

  const rowHeight = isPartialRow
    ? targetRowHeight
    : containerWidth / totalAspect;

  return aspectRatios.map((ar) => ({
    width: Math.floor(ar * rowHeight),
    height: Math.floor(rowHeight),
  }));
}

interface AssetRow {
  assets: AssetItem[];
  isPartial: boolean;
}

/**
 * Pack `assets` into rows so that each full row fills `containerWidth` at
 * approximately `targetHeight`.  The final row is left-aligned (partial).
 */
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

    const rowHeight = containerWidth / aspectSum;
    if (rowHeight <= targetHeight) {
      rows.push({ assets: current, isPartial: false });
      current = [];
      aspectSum = 0;
    }
  }

  if (current.length > 0) {
    rows.push({ assets: current, isPartial: true });
  }

  return rows;
}

// ─── Day grouping ─────────────────────────────────────────────────────────────

interface DayGroup {
  date: string; // ISO date string, e.g. "2024-03-22"
  label: string; // e.g. "Fri 22 Mar 2024"
  locationSummary: string; // e.g. "Amsterdam & 1 more"  or ""
  assets: AssetItem[];
}

function groupByDay(items: AssetItem[]): DayGroup[] {
  const map = new Map<string, AssetItem[]>();
  for (const item of items) {
    const date = item.captured_at
      ? item.captured_at.slice(0, 10)
      : "unknown";
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

    // Collect distinct locality names for this day (skip nulls/empty).
    const localities = Array.from(
      new Set(
        assets
          .map((a) => a.locality)
          .filter((l): l is string => !!l)
      )
    );
    let locationSummary = "";
    if (localities.length === 1) {
      locationSummary = localities[0];
    } else if (localities.length === 2) {
      locationSummary = `${localities[0]} & ${localities[1]}`;
    } else if (localities.length > 2) {
      locationSummary = `${localities[0]} & ${localities.length - 1} more`;
    }

    return { date, label, locationSummary, assets };
  });
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function JustifiedRow({
  row,
  containerWidth,
  token,
  onClickAsset,
}: {
  row: AssetRow;
  containerWidth: number;
  token: string;
  onClickAsset: (id: string) => void;
}) {
  const dims = justifyRow(row.assets, containerWidth, TARGET_ROW_HEIGHT, row.isPartial);
  return (
    <div className="flex gap-0.5">
      {row.assets.map((asset, i) => (
        <MediaCard
          key={asset.id}
          asset={asset}
          width={dims[i].width}
          height={dims[i].height}
          token={token}
          onClick={() => onClickAsset(asset.id)}
        />
      ))}
    </div>
  );
}

function DaySection({
  group,
  containerWidth,
  token,
  onClickAsset,
}: {
  group: DayGroup;
  containerWidth: number;
  token: string;
  onClickAsset: (id: string) => void;
}) {
  const rows = buildRows(group.assets, containerWidth, TARGET_ROW_HEIGHT);
  return (
    <section data-date={group.date} className="mb-6">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-sm font-semibold text-gray-800">{group.label}</h2>
        {group.locationSummary && (
          <span className="text-xs text-gray-400">{group.locationSummary}</span>
        )}
      </div>
      <div className="flex flex-col gap-0.5">
        {rows.map((row, i) => (
          <JustifiedRow
            key={i}
            row={row}
            containerWidth={containerWidth}
            token={token}
            onClickAsset={onClickAsset}
          />
        ))}
      </div>
    </section>
  );
}

// ─── Search results grid (plain grid, no justified layout needed for search) ──

function SearchGrid({
  assets,
  token,
  onClickAsset,
}: {
  assets: AssetItem[];
  token: string;
  onClickAsset: (id: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-0.5 sm:grid-cols-3 lg:grid-cols-5">
      {assets.map((asset) => (
        <div key={asset.id} className="aspect-square overflow-hidden bg-gray-100">
          <MediaCard
            asset={asset}
            width={0}
            height={0}
            token={token}
            onClick={() => onClickAsset(asset.id)}
          />
        </div>
      ))}
    </div>
  );
}

/** Returns the element's offsetTop relative to a given ancestor container. */
function offsetTopWithin(el: HTMLElement, container: HTMLElement): number {
  let top = 0;
  let current: HTMLElement | null = el;
  while (current && current !== container) {
    top += current.offsetTop;
    current = current.offsetParent as HTMLElement | null;
  }
  return top;
}

// ─── Timeline scrubber ────────────────────────────────────────────────────────

interface YearEntry {
  year: number;
  /** Selector that identifies the first day-section element for this year. */
  selector: string;
}

function TimelineScrubber({
  years,
  activeYear,
  onYearClick,
}: {
  years: YearEntry[];
  activeYear: number | null;
  onYearClick: (selector: string, year: number) => void;
}) {
  if (years.length === 0) return null;
  return (
    <div className="hidden md:flex w-10 flex-shrink-0 flex-col items-center justify-between py-4 select-none">
      {years.map(({ year, selector }) => (
        <button
          key={year}
          onClick={() => onYearClick(selector, year)}
          className={`text-xs leading-none transition-colors ${
            activeYear === year
              ? "font-semibold text-gray-900"
              : "text-gray-400 hover:text-gray-700"
          }`}
        >
          {year}
        </button>
      ))}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function Home() {
  const { token, ready, logout } = useAuth();
  const router = useRouter();

  // Timeline state
  const [items, setItems] = useState<AssetItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(undefined);
  const [prevCursor, setPrevCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Search state
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<AssetItem[] | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Layout
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const gridRef = useRef<HTMLDivElement | null>(null);
  // Saved before prepending items — useLayoutEffect restores scroll position after render.
  const scrollAnchor = useRef<{ scrollTop: number; scrollHeight: number } | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [activeYear, setActiveYear] = useState<number | null>(null);
  const [allYears, setAllYears] = useState<number[]>([]);
  const didRestoreScroll = useRef(false);
  const pendingScrollSelector = useRef<string | null>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  // Measure container width for justified layout.
  // Depends on ready+token because the grid div only mounts after auth resolves
  // (the component returns null before that), so the effect must re-run then.
  useLayoutEffect(() => {
    if (!gridRef.current) return;
    setContainerWidth(Math.floor(gridRef.current.getBoundingClientRect().width));
    const obs = new ResizeObserver(([entry]) => {
      setContainerWidth(Math.floor(entry.contentRect.width));
    });
    obs.observe(gridRef.current);
    return () => obs.disconnect();
  }, [ready, token]);

  // Scroll restoration — runs once after first batch renders.
  useEffect(() => {
    if (items.length === 0 || didRestoreScroll.current || !scrollRef.current) return;
    const saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved) {
      scrollRef.current.scrollTop = parseInt(saved, 10);
      sessionStorage.removeItem(SCROLL_KEY);
    }
    didRestoreScroll.current = true;
  }, [items]);

  // After items update: either compensate for prepended items or scroll to pending selector.
  useLayoutEffect(() => {
    // Prepend compensation — adjust scrollTop so the viewport doesn't jump.
    if (scrollAnchor.current && scrollRef.current) {
      const delta = scrollRef.current.scrollHeight - scrollAnchor.current.scrollHeight;
      scrollRef.current.scrollTop = scrollAnchor.current.scrollTop + delta;
      scrollAnchor.current = null;
      return;
    }
    // Year-jump scroll to the target section.
    if (!pendingScrollSelector.current || !scrollRef.current) return;
    const el = scrollRef.current;
    const target = el.querySelector<HTMLElement>(pendingScrollSelector.current);
    if (target) {
      pendingScrollSelector.current = null;
      el.scrollTop = offsetTopWithin(target, el) - 8;
    }
  }, [items]);

  const handleAssetClick = useCallback((id: string) => {
    if (scrollRef.current) {
      sessionStorage.setItem(SCROLL_KEY, String(scrollRef.current.scrollTop));
    }
  }, []);

  const fetchPage = useCallback(
    async (cursor?: string) => {
      if (!token) return;
      setLoading(true);
      setError(null);
      try {
        const page = await getAssets(token, cursor);
        setItems((prev) => (cursor ? [...prev, ...page.items] : page.items));
        setNextCursor(page.next_cursor);
        // prev_cursor is only meaningful on the initial fetch (no cursor) — null there.
        // When appending more pages downward, prevCursor stays as-is.
        if (!cursor) setPrevCursor(page.prev_cursor);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load photos");
      } finally {
        setLoading(false);
      }
    },
    [token]
  );

  const fetchPrevPage = useCallback(
    async (before: string) => {
      if (!token) return;
      setLoading(true);
      setError(null);
      try {
        const page = await getAssets(token, undefined, 50, undefined, before);
        if (page.items.length > 0 && scrollRef.current) {
          scrollAnchor.current = {
            scrollTop: scrollRef.current.scrollTop,
            scrollHeight: scrollRef.current.scrollHeight,
          };
        }
        setItems((prev) => [...page.items, ...prev]);
        setPrevCursor(page.prev_cursor);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load photos");
      } finally {
        setLoading(false);
      }
    },
    [token]
  );

  // Initial fetch
  useEffect(() => {
    if (ready && token && nextCursor === undefined) fetchPage();
  }, [ready, token, nextCursor, fetchPage]);

  // Fetch all years once so the scrubber shows the full timeline from the start.
  useEffect(() => {
    if (!ready || !token) return;
    getAssetYears(token).then(setAllYears);
  }, [ready, token]);

  // Infinite scroll downward — observe bottom sentinel.
  useEffect(() => {
    const el = sentinelRef.current;
    const root = scrollRef.current;
    if (!el || !root) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && nextCursor && !loading) {
          fetchPage(nextCursor);
        }
      },
      { root, rootMargin: "400px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [nextCursor, loading, fetchPage]);

  // Infinite scroll upward — observe top sentinel.
  useEffect(() => {
    const el = topSentinelRef.current;
    const root = scrollRef.current;
    if (!el || !root) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && prevCursor && !loading) {
          fetchPrevPage(prevCursor);
        }
      },
      { root, rootMargin: "400px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [prevCursor, loading, fetchPrevPage]);

  // Debounced search
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!query.trim()) {
      setSearchResults(null);
      setSearchError(null);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      if (!token) return;
      setSearchLoading(true);
      setSearchError(null);
      try {
        const page = await searchAssets(token, query);
        setSearchResults(page.items);
      } catch (e) {
        setSearchError(e instanceof Error ? e.message : "Search failed");
      } finally {
        setSearchLoading(false);
      }
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, token]);

  // Track active year from scroll position.
  const updateActiveYear = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const sections = el.querySelectorAll<HTMLElement>("section[data-date]");
    const containerTop = el.getBoundingClientRect().top;
    const half = el.clientHeight / 2;
    let current: number | null = null;
    for (const section of sections) {
      if (section.getBoundingClientRect().top - containerTop <= half) {
        const date = section.getAttribute("data-date");
        if (date && date !== "unknown") {
          current = parseInt(date.slice(0, 4), 10);
        }
      }
    }
    setActiveYear(current);
  }, []);

  // Attach scroll listener after auth resolves (div not in DOM until ready+token are set).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.addEventListener("scroll", updateActiveYear, { passive: true });
    return () => el.removeEventListener("scroll", updateActiveYear);
  }, [ready, token, updateActiveYear]);

  // Set active year once after first batch of photos renders.
  useEffect(() => {
    if (items.length > 0) updateActiveYear();
  }, [items.length, updateActiveYear]);

  if (!ready || !token) return null;

  const isSearching = query.trim().length > 0;
  const groups = groupByDay(items);

  // Build year list for the scrubber.
  // Use the full list fetched from the API so all years appear from the start,
  // even before infinite scroll has loaded photos from older years.
  // The CSS starts-with selector matches the first loaded section for each year.
  const scrubberYears: YearEntry[] = allYears.map((year) => ({
    year,
    selector: `[data-date^="${year}-"]`,
  }));

  const handleYearClick = async (selector: string, year: number) => {
    const el = scrollRef.current;
    if (!el) return;
    const target = el.querySelector<HTMLElement>(selector);
    if (target) {
      // Year already in DOM — scroll to it.
      el.scrollTop = offsetTopWithin(target, el) - 8;
      return;
    }
    // Year not loaded yet — reset feed to start from the end of that year.
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const page = await getAssets(token, undefined, 50, `${year + 1}-01-01T00:00:00Z`);
      pendingScrollSelector.current = selector;
      setItems(page.items);
      setNextCursor(page.next_cursor);
      setPrevCursor(page.prev_cursor);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load photos");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-white">
      {/* Nav */}
      <div className="flex-shrink-0 px-4 py-3 flex items-center justify-between border-b border-gray-100">
        <span className="text-sm font-semibold text-gray-900">Photos</span>
        <div className="flex items-center gap-4">
          <Link href="/upload" className="text-sm text-gray-500 hover:text-gray-900">
            Upload
          </Link>
          <Link href="/albums" className="text-sm text-gray-500 hover:text-gray-900">
            Albums
          </Link>
          <Link href="/map" className="text-sm text-gray-500 hover:text-gray-900">
            Map
          </Link>
          <button
            onClick={async () => { await logout(); router.push("/login"); }}
            className="text-sm text-gray-500 hover:text-gray-900"
          >
            Log out
          </button>
        </div>
      </div>

      {/* Search bar */}
      <div className="flex-shrink-0 px-4 py-2 border-b border-gray-100">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by description, tag, location, or camera…"
          className="w-full rounded-lg border border-gray-200 bg-gray-50 px-4 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-400 focus:bg-white focus:outline-none"
        />
      </div>

      {/* Content area — scroll container + scrubber side by side */}
      <div className="flex flex-1 overflow-hidden">
        {/* Inner scroll container */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
          {/* Search results */}
          {isSearching && (
            <>
              {searchError && (
                <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700">{searchError}</p>
              )}
              {searchLoading && (
                <p className="py-8 text-center text-sm text-gray-400">Searching…</p>
              )}
              {!searchLoading && searchResults !== null && searchResults.length === 0 && (
                <p className="mt-24 text-center text-gray-400">No results for &ldquo;{query}&rdquo;</p>
              )}
              {!searchLoading && searchResults !== null && searchResults.length > 0 && (
                <SearchGrid assets={searchResults} token={token!} onClickAsset={handleAssetClick} />
              )}
            </>
          )}

          {/* Timeline (hidden while searching) */}
          {!isSearching && (
            <>
              {error && (
                <p className="mb-4 rounded bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>
              )}
              {groups.length === 0 && !loading && (
                <p className="mt-24 text-center text-gray-400">
                  No photos yet. Import your Takeout to get started.
                </p>
              )}
              <div ref={topSentinelRef} className="h-1" />
              <div ref={gridRef}>
                {groups.map((group) => (
                  <DaySection
                    key={group.date}
                    group={group}
                    containerWidth={containerWidth}
                    token={token!}
                    onClickAsset={handleAssetClick}
                  />
                ))}
              </div>
              <div ref={sentinelRef} className="h-1" />
              {loading && (
                <p className="py-8 text-center text-sm text-gray-400">Loading…</p>
              )}
            </>
          )}
        </div>

        {/* Timeline scrubber */}
        {!isSearching && (
          <TimelineScrubber
            years={scrubberYears}
            activeYear={activeYear}
            onYearClick={handleYearClick}
          />
        )}
      </div>
    </div>
  );
}
