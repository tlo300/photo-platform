"use client";

/**
 * /upload — Direct file and folder upload page (issue #91).
 *
 * Phases:
 *   1. Idle: choose files (multi-select) or a folder (webkitdirectory)
 *   2. Uploading: progress bar showing upload %
 *   3. Processing: polls GET /import/jobs/{job_id} every 2 s
 *   4. Done/failed: summary (imported / duplicates / errors)
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import {
  startDirectUpload,
  checkUploadPreflight,
  getImportJob,
  UploadPreflightError,
  type ImportJobStatus,
} from "@/lib/api";

const BATCH_SIZE = 200; // max files per POST request

// Google Takeout sidecar JSONs — always sent to the backend even on re-upload
// so the worker can fix captured_at on existing assets.
// Matches both "photo.jpg.json" and "photo.jpg.supplemental-metadata.json".
const SIDECAR_JSON = /\.(jpg|jpeg|heic|heif|png|webp|gif|mp4|mov|avi|mkv)(\.supplemental-metadata)?\.json$/i;

// Fingerprint a file by its relative path + size — good enough to skip re-uploads on retry.
function fingerprint(file: File): string {
  return `${file.webkitRelativePath || file.name}|${file.size}`;
}
// localStorage key for the folder being uploaded (null for flat file picks).
// Uses localStorage so the cache survives page refreshes.
function uploadCacheKey(files: File[]): string | null {
  const root = files[0]?.webkitRelativePath?.split("/")[0];
  return root ? `upload_done_${root}` : null;
}
function loadDoneSet(key: string | null): Set<string> {
  if (!key) return new Set();
  try {
    return new Set(JSON.parse(localStorage.getItem(key) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}
function saveDoneSet(key: string | null, done: Set<string>) {
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify([...done]));
  } catch { /* storage full — ignore */ }
}

type Mode = "files" | "folder";
type Phase = "idle" | "uploading" | "processing" | "done" | "failed";

function getCacheKeys(): string[] {
  return Object.keys(localStorage).filter((k) => k.startsWith("upload_done_"));
}

export default function UploadPage() {
  const { token, ready } = useAuth();
  const router = useRouter();

  const [mode, setMode] = useState<Mode>("files");
  const [phase, setPhase] = useState<Phase>("idle");
  const [uploadPercent, setUploadPercent] = useState(0);
  const [job, setJob] = useState<ImportJobStatus | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [expandedErrors, setExpandedErrors] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploadStats, setUploadStats] = useState<{
    speed: number; loaded: number; total: number;
  } | null>(null);
  const [skippedCount, setSkippedCount] = useState(0);
  const [hasCachedUploads, setHasCachedUploads] = useState(false);

  useEffect(() => {
    setHasCachedUploads(getCacheKeys().length > 0);
  }, []);

  function handleClearCache() {
    getCacheKeys().forEach((k) => localStorage.removeItem(k));
    setHasCachedUploads(false);
  }

  const filesRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const uploadStartRef = useRef<number>(0);
  const processingStartRef = useRef<number>(0);

  useEffect(() => {
    if (ready && !token) {
      router.replace("/login?from=/upload");
    }
  }, [ready, token, router]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, []);

  function handleModeChange(next: Mode) {
    setMode(next);
    setSelectedFiles([]);
    if (filesRef.current) filesRef.current.value = "";
  }

  function handleFilesChange(e: React.ChangeEvent<HTMLInputElement>) {
    const all = Array.from(e.target.files ?? []);
    // Filter to media files only — browsers ignore accept="" for webkitdirectory
    // picks, so JSON sidecars and other non-media files arrive here unfiltered.
    // On Windows, .json files often report empty type, so we also exclude by
    // extension. Keep other empty-type files (HEIC/HEIF browser behaviour).
    //
    // Exception: in folder mode, allow Google Takeout sidecar JSONs through
    // (e.g. PICT0049.JPG.json) so the backend can use them to correct dates.
    const NON_MEDIA_EXT = /\.(json|xml|html|txt|csv|pdf|zip|db|ini|cfg)$/i;
    const media = all.filter((f) => {
      if (mode === "folder" && SIDECAR_JSON.test(f.name)) return true;
      return (
        !NON_MEDIA_EXT.test(f.name) &&
        (f.type === "" || f.type.startsWith("image/") || f.type.startsWith("video/"))
      );
    });
    setSelectedFiles(media);
  }

  async function handleUpload() {
    if (selectedFiles.length === 0 || !token) return;

    setErrorMessage(null);
    setPhase("uploading");
    setUploadPercent(0);
    setUploadStats(null);
    setSkippedCount(0);
    uploadStartRef.current = Date.now();

    // Skip files already confirmed uploaded in a previous attempt this session.
    const cacheKey = uploadCacheKey(selectedFiles);
    const done = loadDoneSet(cacheKey);

    // Augment with server-side knowledge — survives browser restarts and
    // cleared localStorage.  Fails open: if the request errors, done stays
    // as-is and the upload continues normally.
    const serverDone = await checkUploadPreflight(
      token,
      selectedFiles.map((f) => ({ path: f.webkitRelativePath || f.name, size: f.size })),
    );
    serverDone.forEach((fp) => done.add(fp));

    const pairs = selectedFiles
      .map((f) => ({ file: f, path: f.webkitRelativePath ?? "" }))
      .filter((p) => {
        // Sidecar JSONs carry date metadata for existing assets — always send them
        // even on re-upload so the worker can fix captured_at on duplicates.
        if (SIDECAR_JSON.test(p.file.name)) return true;
        return !done.has(fingerprint(p.file));
      });

    const skipped = selectedFiles.length - pairs.length;
    setSkippedCount(skipped);

    if (pairs.length === 0) {
      // Every file was already uploaded — jump straight to done.
      setJob({ job_id: "", status: "done", total: 0, processed: 0, duplicates: selectedFiles.length, errors: [] });
      setPhase("done");
      return;
    }

    const filesToUpload = pairs.map((p) => p.file);
    const pathsToUpload = pairs.map((p) => p.path);
    const totalBytes = filesToUpload.reduce((sum, f) => sum + f.size, 0);

    // Split into batches so no single POST carries the whole folder.
    const batches: Array<{ files: File[]; paths: string[] }> = [];
    for (let i = 0; i < filesToUpload.length; i += BATCH_SIZE) {
      batches.push({
        files: filesToUpload.slice(i, i + BATCH_SIZE),
        paths: pathsToUpload.slice(i, i + BATCH_SIZE),
      });
    }

    const jobIds: string[] = [];
    let bytesBeforeBatch = 0;

    for (const batch of batches) {
      let jobId: string;
      try {
        jobId = await startDirectUpload(
          token,
          batch.files,
          batch.paths,
          null,
          (_percent, batchLoaded) => {
            const overallLoaded = bytesBeforeBatch + batchLoaded;
            const percent = totalBytes > 0 ? Math.round((overallLoaded / totalBytes) * 100) : 0;
            const elapsed = (Date.now() - uploadStartRef.current) / 1000;
            const speed = elapsed > 0 ? overallLoaded / elapsed : 0;
            setUploadPercent(percent);
            setUploadStats({ speed, loaded: overallLoaded, total: totalBytes });
          }
        );
      } catch (err) {
        if (err instanceof UploadPreflightError) {
          setJob({
            job_id: "",
            status: "failed",
            total: err.errors.length,
            processed: 0,
            duplicates: 0,
            errors: err.errors,
          });
          setPhase("failed");
        } else {
          setErrorMessage(err instanceof Error ? err.message : "Upload failed");
          setPhase("idle");
        }
        return;
      }
      jobIds.push(jobId);

      // Persist fingerprints so a retry can skip this batch.
      // Sidecars are intentionally excluded — they're never skipped on re-upload.
      batch.files.forEach((f) => {
        if (!SIDECAR_JSON.test(f.name)) done.add(fingerprint(f));
      });
      saveDoneSet(cacheKey, done);

      bytesBeforeBatch += batch.files.reduce((sum, f) => sum + f.size, 0);
    }

    setPhase("processing");
    processingStartRef.current = Date.now();
    pollJobs(token, jobIds);
  }

  function pollJobs(authToken: string, jobIds: string[]) {
    async function tick() {
      try {
        const statuses = await Promise.all(jobIds.map((id) => getImportJob(authToken, id)));
        const allDone = statuses.every((s) => s.status === "done" || s.status === "failed");
        const anyFailed = statuses.some((s) => s.status === "failed");
        const knownTotal = statuses.some((s) => s.total != null)
          ? statuses.reduce((sum, s) => sum + (s.total ?? 0), 0)
          : null;
        const aggregated: ImportJobStatus = {
          job_id: jobIds[0],
          status: allDone ? (anyFailed ? "failed" : "done") : "processing",
          total: knownTotal,
          processed: statuses.reduce((sum, s) => sum + s.processed, 0),
          duplicates: statuses.reduce((sum, s) => sum + s.duplicates, 0),
          errors: statuses.flatMap((s) => s.errors),
        };
        setJob(aggregated);
        if (allDone) {
          setPhase(anyFailed ? "failed" : "done");
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

  const errorCount = job?.errors.length ?? 0;
  const importedCount = job ? job.processed - job.duplicates - errorCount : 0;

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-8 bg-white dark:bg-gray-900">
      <h1 className="text-2xl font-bold">Upload Photos &amp; Videos</h1>

      {/* ---- Phase: idle ---- */}
      {phase === "idle" && (
        <div className="flex flex-col items-center gap-4 w-full max-w-sm">
          {errorMessage && (
            <p className="text-sm text-red-600">{errorMessage}</p>
          )}

          {/* Mode toggle */}
          <div className="flex rounded-lg border border-gray-200 overflow-hidden w-full dark:border-gray-700">
            <button
              onClick={() => handleModeChange("files")}
              className={`flex-1 py-2 text-sm font-medium transition-colors ${
                mode === "files"
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
              }`}
            >
              Files
            </button>
            <button
              onClick={() => handleModeChange("folder")}
              className={`flex-1 py-2 text-sm font-medium transition-colors ${
                mode === "folder"
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
              }`}
            >
              Folder
            </button>
          </div>

          {/* File / folder picker */}
          <label
            htmlFor="upload-input"
            className="flex flex-col items-center gap-2 w-full cursor-pointer rounded-lg border-2 border-dashed border-gray-300 p-8 hover:border-gray-400 dark:border-gray-600 dark:hover:border-gray-400"
          >
            {selectedFiles.length > 0 ? (
              <span className="text-sm font-medium text-gray-800 text-center dark:text-gray-200">
                {selectedFiles.length === 1
                  ? selectedFiles[0].name
                  : `${selectedFiles.length} files selected`}
              </span>
            ) : (
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {mode === "folder"
                  ? "Select a folder to upload"
                  : "Select photos or videos"}
              </span>
            )}
          </label>

          {/* Render a single input keyed by mode so React replaces it on switch */}
          {mode === "files" ? (
            <input
              id="upload-input"
              key="files"
              ref={filesRef}
              type="file"
              multiple
              accept="image/*,video/*"
              className="hidden"
              onChange={handleFilesChange}
            />
          ) : (
            <input
              id="upload-input"
              key="folder"
              type="file"
              // @ts-expect-error — webkitdirectory is not in standard TS types
              webkitdirectory=""
              accept="image/*,video/*"
              className="hidden"
              onChange={handleFilesChange}
            />
          )}

          <button
            onClick={handleUpload}
            disabled={selectedFiles.length === 0}
            className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            Upload
          </button>

          {hasCachedUploads && (
            <button
              onClick={handleClearCache}
              className="text-xs text-gray-400 hover:text-gray-600 underline"
            >
              Clear upload cache (re-upload all files next time)
            </button>
          )}
        </div>
      )}

      {/* ---- Phase: uploading ---- */}
      {phase === "uploading" && (
        <div className="flex flex-col items-center gap-3 w-full max-w-sm">
          <p className="text-sm text-gray-600 dark:text-gray-400">Uploading… {uploadPercent}%</p>
          {skippedCount > 0 && (
            <p className="text-xs text-green-600">{skippedCount.toLocaleString()} files skipped (already uploaded)</p>
          )}
          <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700">
            <div
              className="h-3 rounded-full bg-blue-500 transition-all duration-200"
              style={{ width: `${uploadPercent}%` }}
            />
          </div>
          {uploadStats && uploadStats.speed > 0 && (() => {
            const fmt = (b: number) =>
              b >= 1024 * 1024 ? `${(b / (1024 * 1024)).toFixed(1)} MB`
              : b >= 1024 ? `${(b / 1024).toFixed(0)} KB`
              : `${b} B`;
            const fmtSpeed = (b: number) =>
              b >= 1024 * 1024 ? `${(b / (1024 * 1024)).toFixed(1)} MB/s`
              : `${(b / 1024).toFixed(0)} KB/s`;
            const remaining = uploadStats.total - uploadStats.loaded;
            const etaSec = uploadStats.speed > 0 ? remaining / uploadStats.speed : 0;
            const eta = etaSec > 60
              ? `${Math.floor(etaSec / 60)}m ${Math.round(etaSec % 60)}s`
              : `${Math.round(etaSec)}s`;
            return (
              <div className="w-full flex justify-between text-xs text-gray-500 dark:text-gray-400">
                <span>{fmt(uploadStats.loaded)} / {fmt(uploadStats.total)}</span>
                <span>{fmtSpeed(uploadStats.speed)}</span>
                <span>{eta} left</span>
              </div>
            );
          })()}
        </div>
      )}

      {/* ---- Phase: processing ---- */}
      {phase === "processing" && (
        <div className="flex flex-col items-center gap-3 w-full max-w-sm">
          {job && job.total != null ? (() => {
            const pct = Math.round((job.processed / job.total) * 100);
            const elapsed = (Date.now() - processingStartRef.current) / 1000;
            const rate = elapsed > 1 && job.processed > 0 ? job.processed / elapsed : 0;
            const remaining = job.total - job.processed;
            const etaSec = rate > 0 ? remaining / rate : null;
            const eta = etaSec == null ? null
              : etaSec > 60 ? `${Math.floor(etaSec / 60)}m ${Math.round(etaSec % 60)}s`
              : `${Math.round(etaSec)}s`;
            return (
              <>
                <div className="w-full flex justify-between text-sm text-gray-600">
                  <span>Processing…</span>
                  <span>{pct}%</span>
                </div>
                <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700">
                  <div
                    className="h-3 rounded-full bg-green-500 transition-all duration-300"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="w-full flex justify-between text-xs text-gray-500 dark:text-gray-400">
                  <span>{job.processed} / {job.total} files</span>
                  {rate > 0 && <span>{rate.toFixed(1)} files/s</span>}
                  {eta && <span>{eta} left</span>}
                </div>
                {(job.duplicates > 0 || job.errors.length > 0) && (
                  <div className="w-full flex gap-3 text-xs">
                    {job.duplicates > 0 && (
                      <span className="text-gray-400">{job.duplicates} duplicate{job.duplicates !== 1 ? "s" : ""}</span>
                    )}
                    {job.errors.length > 0 && (
                      <span className="text-red-400">{job.errors.length} error{job.errors.length !== 1 ? "s" : ""}</span>
                    )}
                  </div>
                )}
              </>
            );
          })() : (
            <>
              <p className="text-sm text-gray-600">Processing…</p>
              <div className="w-full h-3 rounded-full bg-gray-200 overflow-hidden">
                <div className="h-3 w-1/3 rounded-full bg-green-500 animate-pulse" />
              </div>
              <p className="text-xs text-gray-400 dark:text-gray-500">Waiting for worker to start…</p>
            </>
          )}
        </div>
      )}

      {/* ---- Phase: done / failed ---- */}
      {(phase === "done" || phase === "failed") && job && (
        <div className="flex flex-col gap-4 w-full max-w-sm">
          {phase === "failed" && (
            <p className="text-sm font-medium text-red-600">
              Upload failed. Partial results may have been saved.
            </p>
          )}
          {phase === "done" && (
            <p className="text-sm font-medium text-green-700">Upload complete.</p>
          )}

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

          {errorCount > 0 && (
            <div className="rounded-lg border border-red-100 bg-red-50 dark:border-red-900/40 dark:bg-red-900/20">
              <button
                className="flex w-full items-center justify-between px-4 py-2 text-sm text-red-700 font-medium dark:text-red-400"
                onClick={() => setExpandedErrors((v) => !v)}
              >
                <span>
                  {errorCount} file{errorCount !== 1 ? "s" : ""} failed
                </span>
                <span>{expandedErrors ? "▲" : "▼"}</span>
              </button>
              {expandedErrors && (
                <ul className="max-h-64 overflow-y-auto divide-y divide-red-100 border-t border-red-100 dark:divide-red-900/40 dark:border-red-900/40">
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
