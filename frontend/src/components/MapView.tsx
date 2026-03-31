"use client";

// Leaflet heatmap component.  Must be dynamically imported with { ssr: false }
// because Leaflet references `window` at module evaluation time.
//
// Renders all geotagged photo locations as a heatmap layer over OSM tiles and
// calls onBoundsChange with the current viewport bounds after each pan/zoom.

import { useEffect, useRef, useState } from "react";
import type { MapPoint } from "@/lib/api";

export interface MapBounds {
  minLon: number;
  minLat: number;
  maxLon: number;
  maxLat: number;
}

interface MapViewProps {
  points: MapPoint[];
  onBoundsChange: (bounds: MapBounds) => void;
}

export default function MapView({ points, onBoundsChange }: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mapRef = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const heatRef = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const leafletRef = useRef<any>(null);
  const onBoundsChangeRef = useRef(onBoundsChange);
  onBoundsChangeRef.current = onBoundsChange;

  const [mapReady, setMapReady] = useState(false);

  // Initialise the Leaflet map once the container is mounted.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    let cancelled = false;
    (async () => {
      const L = (await import("leaflet")).default;
      await import("leaflet/dist/leaflet.css");
      // leaflet.heat accesses the global L at evaluation time — expose it before importing.
      (window as unknown as { L: typeof L }).L = L;
      await import("leaflet.heat");

      if (cancelled || !containerRef.current) return;

      leafletRef.current = L;

      const map = L.map(containerRef.current, { preferCanvas: true }).setView([20, 0], 2);

      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }).addTo(map);

      const emitBounds = () => {
        const b = map.getBounds();
        onBoundsChangeRef.current({
          minLon: b.getWest(),
          minLat: b.getSouth(),
          maxLon: b.getEast(),
          maxLat: b.getNorth(),
        });
      };

      map.on("moveend", emitBounds);
      map.on("zoomend", emitBounds);

      mapRef.current = map;
      setMapReady(true);

      // Emit initial bounds after a tick so the map has fully laid out.
      setTimeout(emitBounds, 0);
    })();

    return () => {
      cancelled = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
  }, []);

  // Update the heat layer whenever points change (or when the map first becomes ready).
  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !map || !L) return;

    if (heatRef.current) {
      map.removeLayer(heatRef.current);
      heatRef.current = null;
    }

    if (points.length === 0) return;

    // L.heatLayer is added by leaflet.heat at import time (side effect).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const heat = (L as any).heatLayer(
      points.map((p) => [p.lat, p.lon, 1] as [number, number, number]),
      { radius: 22, blur: 17, maxZoom: 7, minOpacity: 0.3, max: 0.6 }
    );
    heat.addTo(map);
    heatRef.current = heat;
  }, [mapReady, points]);

  return <div ref={containerRef} className="w-full h-full" />;
}
