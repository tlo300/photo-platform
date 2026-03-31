"use client";

/**
 * /import — Google Takeout import page.
 *
 * Three phases:
 *   1. Idle / selecting: file picker + upload button
 *   2. Uploading: progress bar showing upload percentage
 *   3. Processing: polls GET /import/jobs/{job_id} every 2 s until done/failed
 *   4. Complete: summary (imported / duplicates / errors) with expandable error list
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import {
  startTakeoutImport,
  getImportJob,
  type ImportJobStatus,
} from "@/lib/api";

type Phase = "idle" | "uploading" | "processing" | "done" | "failed";

export default function ImportPage() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [phase, setPhase] = useState<Phase>("idle");
  const [uploadPercent, setUploadPercent] = useState(0);
  const [job, setJob] = useState<ImportJobStatus | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [expandedErrors, setExpandedErrors] = useState(false);
  const [selectedFileName, setSelectedFileName] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (ready && !token) {
      router.replace("/login?from=/import");
    }
  }, [ready, token, router]);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, []);

  async function handleUpload() {
    const file = fileRef.current?.files?.[0];
    if (!file || !token) return;

    setErrorMessage(null);
    setPhase("uploading");
    setUploadPercent(0);

    let jobId: string;
    try {
      jobId = await startTakeoutImport(token, file, setUploadPercent);
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Upload failed");
      setPhase("idle");
      return;
    }

    setPhase("processing");
    pollJob(token, jobId);
  }

  function pollJob(authToken: string, jobId: string) {
    async function tick() {
      try {
        const status = await getImportJob(authToken, jobId);
        setJob(status);
        if (status.status === "done" || status.status === "failed") {
          setPhase(status.status);
        } else {
          pollRef.current = setTimeout(tick, 2000);
        }
      } catch (err) {
        setErrorMessage(err instanceof Error ? err.message : "Failed to fetch job status");
        setPhase("failed");
      }
    }
    tick();
  }

  if (!ready || !token) return null;

  // ---- Derived counts (only meaningful when job is set) ----
  const errorCount = job?.errors.length ?? 0;
  const importedCount = job ? job.processed - job.duplicates - errorCount : 0;

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-8 bg-white dark:bg-gray-900">
      <h1 className="text-2xl font-bold">Import Google Takeout</h1>

      {/* ---- Phase: idle ---- */}
      {phase === "idle" && (
        <div className="flex flex-col items-center gap-4 w-full max-w-sm">
          {errorMessage && (
            <p className="text-sm text-red-600">{errorMessage}</p>
          )}
          <label className="flex flex-col items-center gap-2 w-full cursor-pointer rounded-lg border-2 border-dashed border-gray-300 p-8 hover:border-gray-400 dark:border-gray-600 dark:hover:border-gray-400">
            {selectedFileName ? (
              <span className="text-sm font-medium text-gray-800 break-all text-center dark:text-gray-200">{selectedFileName}</span>
            ) : (
              <span className="text-sm text-gray-500 dark:text-gray-400">Select a Google Takeout zip file</span>
            )}
            <input
              ref={fileRef}
              type="file"
              accept=".zip,application/zip,application/x-zip-compressed"
              className="hidden"
              onChange={(e) => setSelectedFileName(e.target.files?.[0]?.name ?? null)}
            />
          </label>
          <button
            onClick={handleUpload}
            className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            Upload &amp; Import
          </button>
        </div>
      )}

      {/* ---- Phase: uploading ---- */}
      {phase === "uploading" && (
        <div className="flex flex-col items-center gap-3 w-full max-w-sm">
          <p className="text-sm text-gray-600 dark:text-gray-400">Uploading… {uploadPercent}%</p>
          <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700">
            <div
              className="h-3 rounded-full bg-blue-500 transition-all duration-200"
              style={{ width: `${uploadPercent}%` }}
            />
          </div>
        </div>
      )}

      {/* ---- Phase: processing ---- */}
      {phase === "processing" && (
        <div className="flex flex-col items-center gap-3 w-full max-w-sm">
          <p className="text-sm text-gray-600 dark:text-gray-400">Processing import…</p>
          {job && job.total != null && (
            <>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {job.processed} / {job.total} files
              </p>
              <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700">
                <div
                  className="h-3 rounded-full bg-green-500 transition-all duration-300"
                  style={{ width: `${Math.round((job.processed / job.total) * 100)}%` }}
                />
              </div>
            </>
          )}
          {job && job.total == null && (
            <div className="w-full h-3 rounded-full bg-gray-200 overflow-hidden dark:bg-gray-700">
              <div className="h-3 w-1/3 rounded-full bg-green-500 animate-pulse" />
            </div>
          )}
        </div>
      )}

      {/* ---- Phase: done / failed ---- */}
      {(phase === "done" || phase === "failed") && job && (
        <div className="flex flex-col gap-4 w-full max-w-sm">
          {phase === "failed" && (
            <p className="text-sm font-medium text-red-600">
              Import failed. Partial results may have been saved.
            </p>
          )}
          {phase === "done" && (
            <p className="text-sm font-medium text-green-700">Import complete.</p>
          )}

          {/* Summary counts */}
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-lg border border-gray-200 py-3 dark:border-gray-700">
              <p className="text-2xl font-bold text-gray-800 dark:text-gray-200">{importedCount}</p>
              <p className="text-xs text-gray-500 mt-1 dark:text-gray-400">Imported</p>
            </div>
            <div className="rounded-lg border border-gray-200 py-3 dark:border-gray-700">
              <p className="text-2xl font-bold text-gray-800 dark:text-gray-200">{job.duplicates}</p>
              <p className="text-xs text-gray-500 mt-1 dark:text-gray-400">Duplicates</p>
            </div>
            <div className="rounded-lg border border-gray-200 py-3 dark:border-gray-700">
              <p className="text-2xl font-bold text-gray-800 dark:text-gray-200">{errorCount}</p>
              <p className="text-xs text-gray-500 mt-1 dark:text-gray-400">Errors</p>
            </div>
          </div>

          {/* Expandable errors */}
          {errorCount > 0 && (
            <div className="rounded-lg border border-red-100 bg-red-50 dark:border-red-900/40 dark:bg-red-900/20">
              <button
                className="flex w-full items-center justify-between px-4 py-2 text-sm text-red-700 font-medium dark:text-red-400"
                onClick={() => setExpandedErrors((v) => !v)}
              >
                <span>{errorCount} file{errorCount !== 1 ? "s" : ""} failed</span>
                <span>{expandedErrors ? "▲" : "▼"}</span>
              </button>
              {expandedErrors && (
                <ul className="divide-y divide-red-100 border-t border-red-100 dark:divide-red-900/40 dark:border-red-900/40">
                  {job.errors.map((e, i) => (
                    <li key={i} className="px-4 py-2">
                      <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                        {e.filename ?? "(unknown file)"}
                      </p>
                      <p className="text-xs text-red-600 mt-0.5 dark:text-red-400">{e.reason}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <button
            onClick={() => router.push("/")}
            className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Go to library
          </button>
        </div>
      )}
    </main>
  );
}
