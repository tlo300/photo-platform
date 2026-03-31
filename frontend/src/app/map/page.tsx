"use client";

// Map page — world heatmap of all geotagged photos.
// Left: Leaflet heatmap (fills remaining space).
// Right: always-visible side panel showing thumbnails of photos in the current viewport.
// Clicking a thumbnail navigates to the asset detail page, which is intercepted
// by the @modal parallel route and shown as an overlay.

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { getMapPoints, getAssetsInBbox, MapPoint, AssetItem } from "@/lib/api";
import type { MapBounds } from "@/components/MapView";

// Dynamically imported to keep Leaflet (which requires window) out of the SSR bundle.
const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => <div className="w-full h-full bg-gray-100 animate-pulse" />,
});

export default function MapPage() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [points, setPoints] = useState<MapPoint[]>([]);
  const [panelItems, setPanelItems] = useState<AssetItem[]>([]);
  const [panelLoading, setPanelLoading] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch all geotagged locations for the heatmap once on load.
  useEffect(() => {
    if (!ready || !token) return;
    getMapPoints(token).then(setPoints).catch(console.error);
  }, [ready, token]);

  // Called by MapView on every pan/zoom end.  Debounced to avoid rapid fire fetches.
  const handleBoundsChange = useCallback(
    (bounds: MapBounds) => {
      if (!token) return;
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(async () => {
        setPanelLoading(true);
        try {
          const page = await getAssetsInBbox(
            token,
            bounds.minLon,
            bounds.minLat,
            bounds.maxLon,
            bounds.maxLat
          );
          setPanelItems(page.items);
        } catch {
          // Leave previous results visible on transient errors.
        } finally {
          setPanelLoading(false);
        }
      }, 400);
    },
    [token]
  );

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-white">
      {/* Nav bar */}
      <header className="flex shrink-0 items-center gap-4 border-b border-gray-100 px-4 py-3">
        <Link href="/" className="text-sm text-gray-500 hover:text-gray-900">
          ← Photos
        </Link>
        <span className="text-sm font-semibold text-gray-900">Map</span>
        <span className="ml-auto text-xs text-gray-400">
          {points.length > 0 ? `${points.length.toLocaleString()} geotagged photos` : ""}
        </span>
      </header>

      {/* Body */}
      <div className="flex min-h-0 flex-1">
        {/* Map */}
        <div className="min-w-0 flex-1">
          {ready && (
            <MapView points={points} onBoundsChange={handleBoundsChange} />
          )}
        </div>

        {/* Side panel */}
        <aside className="flex w-[36rem] shrink-0 flex-col border-l border-gray-100 bg-white">
          {/* Panel header */}
          <div className="shrink-0 border-b border-gray-100 px-3 py-2 text-xs font-medium text-gray-500">
            {panelLoading
              ? "Loading…"
              : panelItems.length === 0
              ? "Pan and zoom to browse photos"
              : `${panelItems.length} photo${panelItems.length === 1 ? "" : "s"} in view`}
          </div>

          {/* Thumbnail grid */}
          <div className="flex-1 overflow-y-auto p-2">
            <div className="grid grid-cols-5 gap-1">
              {panelItems.map((item) => (
                <button
                  key={item.id}
                  onClick={() => router.push(`/assets/${item.id}`)}
                  className="aspect-square overflow-hidden rounded focus:outline-none focus:ring-2 focus:ring-blue-400"
                  title={item.locality ?? item.original_filename}
                >
                  {item.thumbnail_url ? (
                    <img
                      src={item.thumbnail_url}
                      alt={item.original_filename}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="h-full w-full bg-gray-200" />
                  )}
                </button>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
