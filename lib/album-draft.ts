// Drafts kept in this browser per CD until they are saved: what was typed or read from the case in the album
// form, and how the cover was edited. Closing the form or a reload after an update loses neither.
import { parseCoverEdit, type CoverEdit } from "./cover-image";

export type AlbumDraft = { artist: string; title: string; year: string; tracks: string[] };

const ALBUM_DRAFTS = "discdock.album-drafts";
const COVER_EDITS = "discdock.cover-edits";
const KEEP_MS = 14 * 24 * 60 * 60 * 1000;

type Stored = Record<string, Record<string, unknown> & { saved_at: number }>;

function load(storage: string): Stored {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(storage) ?? "{}");
    return value && typeof value === "object" ? value as Stored : {};
  } catch {
    return {};
  }
}

function store(storage: string, drafts: Stored): void {
  const now = Date.now();
  const kept = Object.entries(drafts).filter(([, draft]) => typeof draft?.saved_at === "number" && now - draft.saved_at < KEEP_MS);
  try {
    if (kept.length) window.localStorage.setItem(storage, JSON.stringify(Object.fromEntries(kept)));
    else window.localStorage.removeItem(storage);
  } catch {
    // Without browser storage the forms still work; only the draft is not kept.
  }
}

function save(storage: string, key: string, value: object): void {
  store(storage, { ...load(storage), [key]: { ...value, saved_at: Date.now() } });
}

function forget(storage: string, key: string): void {
  const drafts = load(storage);
  delete drafts[key];
  store(storage, drafts);
}

export function readAlbumDraft(key: string): AlbumDraft | null {
  const draft = load(ALBUM_DRAFTS)[key];
  if (!draft || typeof draft.artist !== "string" || typeof draft.title !== "string" || typeof draft.year !== "string" || !Array.isArray(draft.tracks)) return null;
  return { artist: draft.artist, title: draft.title, year: draft.year, tracks: draft.tracks.map((name) => (typeof name === "string" ? name : "")) };
}

export function writeAlbumDraft(key: string, draft: AlbumDraft): void {
  save(ALBUM_DRAFTS, key, draft);
}

export function clearAlbumDraft(key: string): void {
  forget(ALBUM_DRAFTS, key);
}

export function readCoverEdit(key: string): CoverEdit | null {
  return parseCoverEdit(load(COVER_EDITS)[key]);
}

export function writeCoverEdit(key: string, edit: CoverEdit): void {
  save(COVER_EDITS, key, edit);
}

export function clearCoverEdit(key: string): void {
  forget(COVER_EDITS, key);
}
