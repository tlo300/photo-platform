"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getAssets, searchAssets, AssetItem } from "@/lib/api";

const SCROLL_KEY = "home-scroll-y";

function groupByMonth(items: AssetItem[]): { label: string; assets: AssetItem[] }[] {
  const groups: Map<string, AssetItem[]> = new Map();
  for (const item of items) {
    const label = item.captured_at
      ? new Date(item.captured_at).toLocaleDateString("en-US", {
          year: "numeric",
          month: "long",
        })
      : "Unknown date";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(item);
  }
  return Array.from(groups.entries()).map(([label, assets]) => ({ label, assets }));
}

function AssetGrid({ assets }: { assets: AssetItem[] }) {
  return (
    <div className="grid grid-cols-1 gap-0.5 sm:grid-cols-3 lg:grid-cols-5">
      {assets.map((asset) => (
        <Link
          key={asset.id}
          href={`/assets/${asset.id}`}
          className="aspect-square block overflow-hidden bg-gray-100"
          onClick={() => sessionStorage.setItem(SCROLL_KEY, String(window.scrollY))}
        >
          {asset.thumbnail_url ? (
            <img
              src={asset.thumbnail_url}
              alt={asset.original_filename}
              className="h-full w-full object-cover"
              onError={(e) => {
                e.currentTarget.style.display = "none";
              }}
            />
          ) : (
            <div className="h-full w-full animate-pulse bg-gray-200" />
          )}
        </Link>
      ))}
    </div>
  );
}

export default function Home() {
  const { token, ready } = useAuth();
  const router = useRouter();

  // Timeline state
  const [items, setItems] = useState<AssetItem[]>([]);
  // undefined = initial state (no fetch yet); null = reached last page
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const didRestoreScroll = useRef(false);

  // Search state
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<AssetItem[] | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  // Restore scroll position when returning from a detail page.
  // Runs once after the first batch of items renders.
  useEffect(() => {
    if (items.length === 0 || didRestoreScroll.current) return;
    const saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved) {
      window.scrollTo({ top: parseInt(saved, 10), behavior: "instant" });
      sessionStorage.removeItem(SCROLL_KEY);
    }
    didRestoreScroll.current = true;
  }, [items]);

  const fetchPage = useCallback(
    async (cursor?: string) => {
      if (!token) return;
      setLoading(true);
      setError(null);
      try {
        const page = await getAssets(token, cursor);
        setItems((prev) => (cursor ? [...prev, ...page.items] : page.items));
        setNextCursor(page.next_cursor);
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

  // Infinite scroll — observe sentinel, load next page when it enters view
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && nextCursor && !loading) {
          fetchPage(nextCursor);
        }
      },
      { rootMargin: "400px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [nextCursor, loading, fetchPage]);

  // Debounced search — fires 300 ms after the user stops typing
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

  if (!ready || !token) return null;

  const isSearching = query.trim().length > 0;
  const groups = groupByMonth(items);

  return (
    <main className="min-h-screen bg-white px-4 py-6">
      {/* Nav */}
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-900">Photos</span>
        <div className="flex items-center gap-4">
          <Link href="/upload" className="text-sm text-gray-500 hover:text-gray-900">
            Upload
          </Link>
          <Link href="/albums" className="text-sm text-gray-500 hover:text-gray-900">
            Albums
          </Link>
        </div>
      </div>

      {/* Search bar */}
      <div className="mb-6">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by description, tag, location, or camera…"
          className="w-full rounded-lg border border-gray-200 bg-gray-50 px-4 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-400 focus:bg-white focus:outline-none"
        />
      </div>

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
            <AssetGrid assets={searchResults} />
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

          {groups.map(({ label, assets }) => (
            <section key={label} className="mb-8">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
                {label}
              </h2>
              <AssetGrid assets={assets} />
            </section>
          ))}

          <div ref={sentinelRef} className="h-1" />

          {loading && (
            <p className="py-8 text-center text-sm text-gray-400">Loading…</p>
          )}
        </>
      )}
    </main>
  );
}
