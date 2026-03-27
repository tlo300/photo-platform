"use client";

/**
 * MediaCard — renders a single asset thumbnail inside the justified grid.
 * For Live Photos it overlays a badge and swaps to a looping video on
 * desktop hover or mobile long-press (>300 ms).
 */

import { useRef, useState } from "react";
import Link from "next/link";
import { AssetItem, getAsset } from "@/lib/api";

export interface MediaCardProps {
  asset: AssetItem;
  width: number;
  height: number;
  token: string;
  onClick: () => void;
}

export function MediaCard({ asset, width, height, token, onClick }: MediaCardProps) {
  const [liveVideoUrl, setLiveVideoUrl] = useState<string | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const videoFetchedRef = useRef(false);
  const longPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function fetchAndPlay() {
    if (asset.is_live_photo) {
      if (!videoFetchedRef.current) {
        videoFetchedRef.current = true;
        try {
          const detail = await getAsset(token, asset.id);
          if (detail.live_video_url) {
            setLiveVideoUrl(detail.live_video_url);
          }
        } catch {
          // Fail gracefully — stay on still image.
        }
      }
      setIsPlaying(true);
    }
  }

  function stopPlaying() {
    setIsPlaying(false);
    if (longPressTimerRef.current) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
  }

  // When width/height are 0 the card fills its parent (used in search grid).
  const sizeStyle =
    width > 0 && height > 0
      ? { width, height, flexShrink: 0 as const }
      : { width: "100%", height: "100%" };

  return (
    <Link
      href={`/assets/${asset.id}`}
      onClick={onClick}
      style={sizeStyle}
      className="relative block overflow-hidden bg-gray-100"
      // Desktop hover
      onMouseEnter={asset.is_live_photo ? fetchAndPlay : undefined}
      onMouseLeave={asset.is_live_photo ? stopPlaying : undefined}
      // Mobile long-press
      onTouchStart={
        asset.is_live_photo
          ? () => {
              longPressTimerRef.current = setTimeout(fetchAndPlay, 300);
            }
          : undefined
      }
      onTouchEnd={asset.is_live_photo ? stopPlaying : undefined}
      onTouchMove={asset.is_live_photo ? stopPlaying : undefined}
    >
      {isPlaying && liveVideoUrl ? (
        <video
          autoPlay
          loop
          muted
          playsInline
          className="absolute inset-0 h-full w-full object-cover"
          src={liveVideoUrl}
        />
      ) : asset.thumbnail_url ? (
        <img
          src={asset.thumbnail_url}
          alt={asset.original_filename}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
          onError={(e) => {
            e.currentTarget.style.display = "none";
          }}
        />
      ) : (
        <div className="h-full w-full animate-pulse bg-gray-200" />
      )}

      {asset.is_live_photo && (
        <span className="absolute bottom-1 left-1 rounded-full bg-black/40 px-1 text-xs text-white">
          ▶ LIVE
        </span>
      )}
    </Link>
  );
}
