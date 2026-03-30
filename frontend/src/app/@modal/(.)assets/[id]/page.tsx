"use client";

// Intercepting route — renders the asset detail page as a modal overlay.
// Triggered when navigating to /assets/[id] from within the app (feed or album).
// Direct URL access still shows the full standalone page at app/assets/[id]/page.tsx.

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import AssetDetailPage from "@/app/assets/[id]/page";

export default function PhotoModal() {
  const router = useRouter();

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") router.back();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [router]);

  return (
    <div className="fixed inset-0 z-50">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/80"
        onClick={() => router.back()}
      />

      {/* Scrollable area so tall content (metadata sidebar) is reachable */}
      <div className="absolute inset-0 overflow-y-auto">
        <div className="flex min-h-full items-start justify-center px-4 py-8">
          <div
            className="relative w-full max-w-6xl overflow-hidden rounded-lg bg-white shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <AssetDetailPage />
          </div>
        </div>
      </div>
    </div>
  );
}
