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
  getImportJob,
  type ImportJobStatus,
} from "@/lib/api";

type Mode = "files" | "folder";
type Phase = "idle" | "uploading" | "processing" | "done" | "failed";

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
    const NON_MEDIA_EXT = /\.(json|xml|html|txt|csv|pdf|zip|db|ini|cfg)$/i;
    const media = all.filter(
      (f) =>
        !NON_MEDIA_EXT.test(f.name) &&
        (f.type === "" || f.type.startsWith("image/") || f.type.startsWith("video/"))
    );
    setSelectedFiles(media);
  }

  async function handleUpload() {
    if (selectedFiles.length === 0 || !token) return;

    setErrorMessage(null);
    setPhase("uploading");
    setUploadPercent(0);
    setUploadStats(null);
    uploadStartRef.current = Date.now();

    // For folder uploads, preserve the relative paths; for file uploads these are empty.
    const paths = selectedFiles.map((f) => f.webkitRelativePath ?? "");

    let jobId: string;
    try {
      jobId = await startDirectUpload(token, selectedFiles, paths, null, (percent, loaded, total) => {
        setUploadPercent(percent);
        const elapsed = (Date.now() - uploadStartRef.current) / 1000;
        const speed = elapsed > 0 ? loaded / elapsed : 0;
        setUploadStats({ speed, loaded, total });
      });
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Upload failed");
      setPhase("idle");
      return;
    }

    setPhase("processing");
    processingStartRef.current = Date.now();
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
        setErrorMessage(
          err instanceof Error ? err.message : "Failed to fetch job status"
        );
        setPhase("failed");
      }
    }
    tick();
  }

  if (!ready || !token) return null;

  const errorCount = job?.errors.length ?? 0;
  const importedCount = job ? job.processed - job.duplicates - errorCount : 0;

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-8">
      <h1 className="text-2xl font-bold">Upload Photos &amp; Videos</h1>

      {/* ---- Phase: idle ---- */}
      {phase === "idle" && (
        <div className="flex flex-col items-center gap-4 w-full max-w-sm">
          {errorMessage && (
            <p className="text-sm text-red-600">{errorMessage}</p>
          )}

          {/* Mode toggle */}
          <div className="flex rounded-lg border border-gray-200 overflow-hidden w-full">
            <button
              onClick={() => handleModeChange("files")}
              className={`flex-1 py-2 text-sm font-medium transition-colors ${
                mode === "files"
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Files
            </button>
            <button
              onClick={() => handleModeChange("folder")}
              className={`flex-1 py-2 text-sm font-medium transition-colors ${
                mode === "folder"
                  ? "bg-blue-600 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              Folder
            </button>
          </div>

          {/* File / folder picker */}
          <label
            htmlFor="upload-input"
            className="flex flex-col items-center gap-2 w-full cursor-pointer rounded-lg border-2 border-dashed border-gray-300 p-8 hover:border-gray-400"
          >
            {selectedFiles.length > 0 ? (
              <span className="text-sm font-medium text-gray-800 text-center">
                {selectedFiles.length === 1
                  ? selectedFiles[0].name
                  : `${selectedFiles.length} files selected`}
              </span>
            ) : (
              <span className="text-sm text-gray-500">
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
        </div>
      )}

      {/* ---- Phase: uploading ---- */}
      {phase === "uploading" && (
        <div className="flex flex-col items-center gap-3 w-full max-w-sm">
          <p className="text-sm text-gray-600">Uploading… {uploadPercent}%</p>
          <div className="w-full h-3 rounded-full bg-gray-200">
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
              <div className="w-full flex justify-between text-xs text-gray-500">
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
                <div className="w-full h-3 rounded-full bg-gray-200">
                  <div
                    className="h-3 rounded-full bg-green-500 transition-all duration-300"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="w-full flex justify-between text-xs text-gray-500">
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
              <p className="text-xs text-gray-400">Waiting for worker to start…</p>
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
            <div className="rounded-lg border border-gray-200 py-3">
              <p className="text-2xl font-bold text-gray-800">{importedCount}</p>
              <p className="text-xs text-gray-500 mt-1">Imported</p>
            </div>
            <div className="rounded-lg border border-gray-200 py-3">
              <p className="text-2xl font-bold text-gray-800">{job.duplicates}</p>
              <p className="text-xs text-gray-500 mt-1">Duplicates</p>
            </div>
            <div className="rounded-lg border border-gray-200 py-3">
              <p className="text-2xl font-bold text-gray-800">{errorCount}</p>
              <p className="text-xs text-gray-500 mt-1">Errors</p>
            </div>
          </div>

          {errorCount > 0 && (
            <div className="rounded-lg border border-red-100 bg-red-50">
              <button
                className="flex w-full items-center justify-between px-4 py-2 text-sm text-red-700 font-medium"
                onClick={() => setExpandedErrors((v) => !v)}
              >
                <span>
                  {errorCount} file{errorCount !== 1 ? "s" : ""} failed
                </span>
                <span>{expandedErrors ? "▲" : "▼"}</span>
              </button>
              {expandedErrors && (
                <ul className="divide-y divide-red-100 border-t border-red-100">
                  {job.errors.map((e, i) => (
                    <li key={i} className="px-4 py-2">
                      <p className="text-sm font-medium text-gray-700">
                        {e.filename ?? "(unknown file)"}
                      </p>
                      <p className="text-xs text-red-600 mt-0.5">{e.reason}</p>
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
