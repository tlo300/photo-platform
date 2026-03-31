"use client";

// People index page — grid of everyone tagged in photos via Google Takeout.
// Route: /people

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { listPeople, PersonItem } from "@/lib/api";
import { ThemeToggle } from "@/components/ThemeToggle";

export default function PeoplePage() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [people, setPeople] = useState<PersonItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  useEffect(() => {
    if (!ready || !token) return;
    setLoading(true);
    listPeople(token)
      .then(setPeople)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load people"))
      .finally(() => setLoading(false));
  }, [ready, token]);

  if (!ready || !token) return null;

  return (
    <main className="min-h-screen bg-white px-4 py-6 dark:bg-gray-900">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link
            href="/"
            className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
              <path fillRule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clipRule="evenodd" />
            </svg>
            Photos
          </Link>
          <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">People</h1>
        </div>
        <ThemeToggle />
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
      {!loading && people.length === 0 && !error && (
        <p className="mt-24 text-center text-gray-400 dark:text-gray-500">
          No people found. Import photos from Google Takeout to see people here.
        </p>
      )}

      {/* People grid */}
      {!loading && people.length > 0 && (
        <div className="grid grid-cols-2 gap-6 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
          {people.map((person) => (
            <Link key={person.id} href={`/people/${person.id}`} className="group block">
              {/* Avatar — circular crop */}
              <div className="mx-auto aspect-square w-full max-w-[160px] overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                {person.cover_thumbnail_url ? (
                  <img
                    src={person.cover_thumbnail_url}
                    alt={person.name}
                    className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-105"
                    onError={(e) => {
                      e.currentTarget.style.display = "none";
                    }}
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-gray-300 dark:text-gray-600">
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      viewBox="0 0 24 24"
                      fill="currentColor"
                      className="h-16 w-16"
                    >
                      <path
                        fillRule="evenodd"
                        d="M7.5 6a4.5 4.5 0 1 1 9 0 4.5 4.5 0 0 1-9 0ZM3.751 20.105a8.25 8.25 0 0 1 16.498 0 .75.75 0 0 1-.437.695A18.683 18.683 0 0 1 12 22.5c-2.786 0-5.433-.608-7.812-1.7a.75.75 0 0 1-.437-.695Z"
                        clipRule="evenodd"
                      />
                    </svg>
                  </div>
                )}
              </div>

              {/* Name + count */}
              <div className="mt-3 text-center">
                <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
                  {person.name}
                </p>
                <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                  {person.photo_count} {person.photo_count === 1 ? "photo" : "photos"}
                </p>
              </div>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}
