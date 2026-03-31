"use client";

/**
 * Horizontal filter bar for the photo overview page.
 * Fully controlled — all state lives in the parent via URL params.
 */

export interface GalleryFilters {
  type: "photo" | "video" | null;
  hasLocation: boolean;
  liveOnly: boolean;
}

export function FilterBar({
  filters,
  onChange,
}: {
  filters: GalleryFilters;
  onChange: (f: GalleryFilters) => void;
}) {
  const pill =
    "px-3 py-1 rounded-full text-xs font-medium transition-colors cursor-pointer select-none";
  const active =
    "bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900";
  const inactive =
    "border border-gray-300 text-gray-600 hover:border-gray-500 hover:text-gray-900 dark:border-gray-600 dark:text-gray-400 dark:hover:border-gray-400 dark:hover:text-gray-200";

  return (
    <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2 border-b border-gray-100 dark:border-gray-800 overflow-x-auto">
      {/* Media type pills — mutually exclusive */}
      <button
        className={`${pill} ${filters.type === null ? active : inactive}`}
        onClick={() => onChange({ ...filters, type: null })}
      >
        All
      </button>
      <button
        className={`${pill} ${filters.type === "photo" ? active : inactive}`}
        onClick={() => onChange({ ...filters, type: "photo" })}
      >
        Photos
      </button>
      <button
        className={`${pill} ${filters.type === "video" ? active : inactive}`}
        onClick={() => onChange({ ...filters, type: "video" })}
      >
        Videos
      </button>

      <div className="w-px h-4 bg-gray-200 dark:bg-gray-700 flex-shrink-0" />

      {/* Toggle chips */}
      <button
        className={`${pill} ${filters.hasLocation ? active : inactive}`}
        onClick={() => onChange({ ...filters, hasLocation: !filters.hasLocation })}
      >
        Has location
      </button>
      <button
        className={`${pill} ${filters.liveOnly ? active : inactive}`}
        onClick={() => onChange({ ...filters, liveOnly: !filters.liveOnly })}
      >
        Live Photos
      </button>
    </div>
  );
}
