# Folder (Album) Deletion — Design Spec
**Issue:** #183  
**Date:** 2026-04-01

## Summary

Add a Delete button to the album detail page. A confirmation modal lets the user optionally delete photos that are exclusively in this album (not in any other album). Photos shared across multiple albums are never deleted.

## Backend

### 1. `GET /albums/{id}` — add `exclusive_asset_count`

Add `exclusive_asset_count: int` to `AlbumResponse`. Computed with a subquery:

```sql
SELECT COUNT(*) FROM album_assets aa
WHERE aa.album_id = :this_album_id
  AND NOT EXISTS (
    SELECT 1 FROM album_assets aa2
    WHERE aa2.asset_id = aa.asset_id
      AND aa2.album_id != :this_album_id
  )
```

Returns `0` when the album is empty or all assets belong to at least one other album.

### 2. `DELETE /albums/{id}` — add `delete_exclusive_assets` param

New optional query param: `delete_exclusive_assets: bool = False`.

When `True`:
1. Query all asset IDs exclusive to this album (same subquery logic as the count).
2. Collect all storage keys for those assets: original file, live video, thumbnails (`thumb.webp`, `display.webp`), `asset.json`, `pair.json` — 6 keys per asset.
3. Batch-delete storage keys via `StorageService.delete_objects()` in chunks of 1000 (best-effort; failures logged, not fatal).
4. Delete exclusive asset DB rows with `DELETE WHERE asset_id IN (...)` — FK cascade removes `MediaMetadata`, `Location`, `AlbumAsset`, `AssetTag`.
5. Delete the album row — FK cascade removes any remaining `AlbumAsset` rows (for shared assets).

When `False` (default): existing behaviour — album deleted, all assets untouched.

Returns `204 No Content` in both cases.

### 3. `StorageService.delete_objects(keys: list[str])`

New method on `StorageService`. Uses the S3 `delete_objects` API (up to 1000 keys per call). Handles chunking internally. Best-effort: logs warnings on failure, does not raise. Replaces per-key iteration for bulk deletion.

### 4. No migration needed

No schema changes.

## Frontend

### `api.ts`

- Add `exclusive_asset_count: number` to `AlbumItem`.
- Add `deleteAlbum(token: string, albumId: string, deleteExclusiveAssets: boolean): Promise<void>` — sends `DELETE /albums/{id}?delete_exclusive_assets=true|false`.

### Album detail page (`/albums/[id]/page.tsx`)

- Add a "Delete album" button in the page header, next to the existing hide/show toggle.
- Clicking it opens the delete modal.

### Delete modal

State: `showDeleteModal: boolean`, `deleting: boolean`, `deleteError: string | null`, `deleteExclusive: boolean` (checkbox state, default `false`).

Content:
- Title: **Delete album?**
- Body: `"<album title>" will be permanently deleted. This cannot be undone.`
- Checkbox (only rendered when `exclusive_asset_count > 0`): `Also delete <N> photos that are only in this album and nowhere else`
- Buttons: **Cancel** (resets error, closes modal) | **Delete album** (disabled + "Deleting…" while in flight)
- Inline error display on failure (modal stays open)

After success: `router.push("/albums")`.

## Error handling

- Storage batch-delete failures are logged and non-fatal — same policy as single-asset delete.
- If the album is not found or belongs to another user, the endpoint returns `404` (RLS-enforced).
- Frontend shows inline error in the modal on any non-2xx response.

## Testing

- Backend: `tests/test_delete_album.py`
  - Delete album only (assets remain)
  - Delete album + exclusive assets (assets + storage keys gone)
  - Delete album + exclusive assets when some assets are shared (shared assets remain)
  - `exclusive_asset_count` correct on `GET /albums/{id}` (zero, non-zero, mixed)
  - 404 for missing album
  - RLS isolation (cannot delete another user's album)
  - Unauthenticated request returns 401
- Frontend: manual happy path + error path (API failure shows error in modal)
