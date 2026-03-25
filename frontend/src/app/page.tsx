"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getAssets, AssetItem } from "@/lib/api";

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

export default function Home() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [items, setItems] = useState<AssetItem[]>([]);
  // undefined = initial state (no fetch yet); null = reached last page
  const [nextCursor, setNextCursor] = useState<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

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

  if (!ready || !token) return null;

  const groups = groupByMonth(items);

  return (
    <main className="min-h-screen bg-white px-4 py-6">
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
          <div className="grid grid-cols-1 gap-0.5 sm:grid-cols-3 lg:grid-cols-5">
            {assets.map((asset) => (
              <Link
                key={asset.id}
                href={`/assets/${asset.id}`}
                className="aspect-square block overflow-hidden bg-gray-100"
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
        </section>
      ))}

      <div ref={sentinelRef} className="h-1" />

      {loading && (
        <p className="py-8 text-center text-sm text-gray-400">Loading…</p>
      )}
    </main>
  );
}
