// API_URL is used by server components (internal Docker network).
// NEXT_PUBLIC_API_URL is used by the browser (goes through Caddy).
const API_BASE_URL =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Browser-only base URL — never use API_URL here as it is not exposed to the client.
const CLIENT_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE_URL}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export async function login(email: string, password: string): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Login failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function register(
  email: string,
  displayName: string,
  password: string
): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, display_name: displayName, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Registration failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${CLIENT_API_URL}/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  } catch {
    // Best-effort — client state is cleared regardless.
  }
}

export async function refresh(): Promise<string | null> {
  try {
    const res = await fetch(`${CLIENT_API_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (!res.ok) return null;
    const data = await res.json();
    return (data as { access_token: string }).access_token;
  } catch {
    return null;
  }
}

export async function registerWithInvitation(
  invitationToken: string,
  email: string,
  displayName: string,
  password: string
): Promise<string> {
  const res = await fetch(`${CLIENT_API_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      email,
      display_name: displayName,
      password,
      invitation_token: invitationToken,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Registration failed");
  }
  const data = await res.json();
  return (data as { access_token: string }).access_token;
}

export async function getInvite(token: string): Promise<{ email: string }> {
  const res = await fetch(`${CLIENT_API_URL}/auth/invite/${token}`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Invalid invitation");
  }
  return res.json();
}

export interface InvitationOut {
  id: string;
  email: string;
  expires_at: string;
  created_at: string;
}

export interface InvitationsPage {
  total: number;
  page: number;
  page_size: number;
  items: InvitationOut[];
}

export async function listInvitations(
  token: string,
  page = 1
): Promise<InvitationsPage> {
  const res = await fetch(
    `${CLIENT_API_URL}/admin/invitations?page=${page}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load invitations");
  }
  return res.json();
}

export interface FileError {
  filename: string | null;
  reason: string;
}

export interface ImportJobStatus {
  job_id: string;
  status: "pending" | "processing" | "done" | "failed";
  total: number | null;
  processed: number;
  duplicates: number;
  errors: FileError[];
}

/** Thrown by startDirectUpload when the server rejects every file before creating a job. */
export class UploadPreflightError extends Error {
  errors: FileError[];
  constructor(errors: FileError[]) {
    super(`${errors.length} file(s) failed validation`);
    this.name = "UploadPreflightError";
    this.errors = errors;
  }
}

export function startTakeoutImport(
  token: string,
  file: File,
  onUploadProgress: (percent: number) => void
): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("file", file);

    xhr.open("POST", `${CLIENT_API_URL}/import/takeout`);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status === 202) {
        try {
          const data = JSON.parse(xhr.responseText) as { job_id: string };
          resolve(data.job_id);
        } catch {
          reject(new Error("Unexpected response from server"));
        }
      } else if (xhr.status === 422) {
        try {
          const data = JSON.parse(xhr.responseText) as { errors?: FileError[] };
          reject(new UploadPreflightError(data.errors ?? []));
        } catch {
          reject(new Error(`Upload failed (${xhr.status})`));
        }
      } else {
        try {
          const data = JSON.parse(xhr.responseText) as { detail?: string };
          reject(new Error(data.detail ?? `Upload failed (${xhr.status})`));
        } catch {
          reject(new Error(`Upload failed (${xhr.status})`));
        }
      }
    });

    xhr.addEventListener("error", () => reject(new Error("Network error during upload")));
    xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));

    xhr.send(form);
  });
}

export async function getImportJob(
  token: string,
  jobId: string
): Promise<ImportJobStatus> {
  const res = await fetch(`${CLIENT_API_URL}/import/jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to fetch job status");
  }
  return res.json();
}

export function startDirectUpload(
  token: string,
  files: File[],
  paths: string[],
  albumId: string | null,
  onUploadProgress: (percent: number, loaded: number, total: number) => void
): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();

    for (const file of files) {
      form.append("files", file);
    }
    for (const p of paths) {
      form.append("paths", p);
    }

    const url = albumId
      ? `${CLIENT_API_URL}/upload?album_id=${encodeURIComponent(albumId)}`
      : `${CLIENT_API_URL}/upload`;

    xhr.open("POST", url);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100), e.loaded, e.total);
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status === 202) {
        try {
          const data = JSON.parse(xhr.responseText) as { job_id: string };
          resolve(data.job_id);
        } catch {
          reject(new Error("Unexpected response from server"));
        }
      } else {
        try {
          const data = JSON.parse(xhr.responseText) as { detail?: string };
          reject(new Error(data.detail ?? `Upload failed (${xhr.status})`));
        } catch {
          reject(new Error(`Upload failed (${xhr.status})`));
        }
      }
    });

    xhr.addEventListener("error", () => reject(new Error("Network error during upload")));
    xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));

    xhr.send(form);
  });
}

/**
 * Check which files from the given list already exist in the user's library.
 * Returns a Set of "path|size" fingerprints (same format as the upload page's
 * fingerprint() function) for files that can be skipped.  Fails open — if the
 * request errors for any reason the returned Set is empty so the upload
 * continues normally.
 */
export async function checkUploadPreflight(
  token: string,
  files: Array<{ path: string; size: number }>,
): Promise<Set<string>> {
  if (files.length === 0) return new Set();
  try {
    const res = await fetch(`${CLIENT_API_URL}/upload/preflight`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ files }),
    });
    if (!res.ok) return new Set();
    const data = (await res.json()) as { already_uploaded: string[] };
    return new Set(data.already_uploaded);
  } catch {
    return new Set();
  }
}

export interface AssetItem {
  id: string;
  original_filename: string;
  mime_type: string;
  captured_at: string | null;
  thumbnail_ready: boolean;
  thumbnail_url: string | null;
  width: number | null;
  height: number | null;
  locality: string | null;
  is_live_photo: boolean;
}

export interface AssetsPage {
  items: AssetItem[];
  next_cursor: string | null;
  prev_cursor: string | null;
}

export async function getAssets(
  token: string,
  cursor?: string,
  limit = 50,
  dateTo?: string,
  before?: string,
  personId?: string,
  mediaType?: "photo" | "video",
  hasLocation?: boolean,
  isLivePhoto?: boolean,
): Promise<AssetsPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  if (dateTo) params.set("date_to", dateTo);
  if (before) params.set("before", before);
  if (personId) params.set("person_id", personId);
  if (mediaType) params.set("media_type", mediaType);
  if (hasLocation !== undefined) params.set("has_location", String(hasLocation));
  if (isLivePhoto !== undefined) params.set("is_live_photo", String(isLivePhoto));
  const res = await fetch(`${CLIENT_API_URL}/assets?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load assets");
  }
  return res.json();
}

export interface AssetMetadata {
  make: string | null;
  model: string | null;
  width_px: number | null;
  height_px: number | null;
  duration_seconds: number | null;
  iso: number | null;
  aperture: number | null;
  shutter_speed: number | null;
  focal_length: number | null;
  flash: boolean | null;
}

export interface AssetLocation {
  latitude: number;
  longitude: number;
  altitude_metres: number | null;
  display_name: string | null;
  country: string | null;
}

export interface AssetTagItem {
  name: string;
  source: string | null;
}

export interface AssetDetail {
  id: string;
  original_filename: string;
  mime_type: string;
  captured_at: string | null;
  file_size_bytes: number;
  description: string | null;
  full_url: string;
  thumbnail_url: string | null;
  display_url: string | null;
  is_live_photo: boolean;
  live_video_url: string | null;
  metadata: AssetMetadata | null;
  location: AssetLocation | null;
  tags: AssetTagItem[];
}

export interface MapPoint {
  id: string;
  lat: number;
  lon: number;
}

export async function getMapPoints(token: string): Promise<MapPoint[]> {
  const res = await fetch(`${CLIENT_API_URL}/map/points`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load map points");
  }
  return res.json();
}

export async function getAssetsInBbox(
  token: string,
  minLon: number,
  minLat: number,
  maxLon: number,
  maxLat: number,
  limit = 100
): Promise<AssetsPage> {
  const bbox = `${minLon},${minLat},${maxLon},${maxLat}`;
  const params = new URLSearchParams({ bbox, limit: String(limit) });
  const res = await fetch(`${CLIENT_API_URL}/assets?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load assets");
  }
  return res.json();
}

export async function getAssetYears(token: string): Promise<number[]> {
  const res = await fetch(`${CLIENT_API_URL}/assets/years`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return [];
  return res.json();
}

export async function searchAssets(
  token: string,
  q: string,
  limit = 50
): Promise<AssetsPage> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const res = await fetch(`${CLIENT_API_URL}/assets/search?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Search failed");
  }
  return res.json();
}

export async function getAsset(token: string, id: string): Promise<AssetDetail> {
  const res = await fetch(`${CLIENT_API_URL}/assets/${id}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load asset");
  }
  return res.json();
}

export interface AdjacentAssets {
  prev_id: string | null; // newer photo (up the timeline)
  next_id: string | null; // older photo (down the timeline)
}

export async function getAdjacentAssets(token: string, id: string): Promise<AdjacentAssets> {
  const res = await fetch(`${CLIENT_API_URL}/assets/${id}/adjacent`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return { prev_id: null, next_id: null };
  return res.json();
}

export interface AlbumItem {
  id: string;
  title: string;
  description: string | null;
  parent_id: string | null;
  cover_asset_id: string | null;
  cover_thumbnail_url: string | null;
  asset_count: number;
  is_hidden: boolean;
  created_at: string;
}

export interface AlbumAssetItem {
  id: string;
  original_filename: string;
  mime_type: string;
  captured_at: string | null;
  thumbnail_ready: boolean;
  thumbnail_url: string | null;
  width: number | null;
  height: number | null;
  is_live_photo: boolean;
  locality: string | null;
}

export type AlbumSort = "title" | "last_modified" | "recent_photo";

export async function listAlbums(token: string, sort: AlbumSort = "title"): Promise<AlbumItem[]> {
  const res = await fetch(`${CLIENT_API_URL}/albums?sort=${sort}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load albums");
  }
  return res.json();
}

export async function createAlbum(token: string, title: string): Promise<AlbumItem> {
  const res = await fetch(`${CLIENT_API_URL}/albums`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to create album");
  }
  return res.json();
}

export async function updateAlbumCover(
  token: string,
  albumId: string,
  coverAssetId: string
): Promise<AlbumItem> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ cover_asset_id: coverAssetId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to update album cover");
  }
  return res.json();
}

export async function updateAlbumHidden(
  token: string,
  albumId: string,
  isHidden: boolean
): Promise<AlbumItem> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ is_hidden: isHidden }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to update album");
  }
  return res.json();
}

export async function getAlbumAssets(
  token: string,
  albumId: string
): Promise<AlbumAssetItem[]> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}/assets`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load album assets");
  }
  return res.json();
}

export async function addAssetsToAlbum(
  token: string,
  albumId: string,
  assetIds: string[]
): Promise<void> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}/assets`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ asset_ids: assetIds }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to add to album");
  }
}

export interface AssetAlbumItem {
  id: string;
  title: string;
}

export async function getAssetAlbums(
  token: string,
  assetId: string
): Promise<AssetAlbumItem[]> {
  const res = await fetch(`${CLIENT_API_URL}/assets/${assetId}/albums`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load asset albums");
  }
  return res.json();
}

export async function removeAssetFromAlbum(
  token: string,
  albumId: string,
  assetId: string
): Promise<void> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}/assets/${assetId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to remove from album");
  }
}

export interface PersonItem {
  id: string;
  name: string;
  photo_count: number;
  cover_thumbnail_url: string | null;
}

export async function listPeople(token: string): Promise<PersonItem[]> {
  const res = await fetch(`${CLIENT_API_URL}/people`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load people");
  }
  return res.json();
}

export async function renamePerson(
  token: string,
  id: string,
  name: string
): Promise<{ id: string; name: string }> {
  const res = await fetch(`${CLIENT_API_URL}/people/${id}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    if (res.status === 409) throw new Error("A person with that name already exists");
    throw new Error((data as { detail?: string }).detail ?? "Failed to rename person");
  }
  return res.json();
}

export async function createInvitation(
  token: string,
  email: string
): Promise<{ invitation_token: string; email: string; expires_at: string }> {
  const res = await fetch(`${CLIENT_API_URL}/admin/invitations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to create invitation");
  }
  return res.json();
}
