export type DiscKind = "bluray" | "dvd" | "audio_cd" | "data" | "unknown";
export type MediaKind = "movie" | "series" | "music" | "other" | "data" | "unknown";

export type MetadataCandidate = {
  provider: string;
  provider_id: string;
  title: string;
  year: string;
  media_kind: MediaKind;
  poster_url: string;
  plot: string;
  runtime_minutes: number;
  user_selected?: boolean;
};

export type Drive = {
  id: string;
  letter: string;
  name: string;
  pnp_device_id: string;
  media_loaded: boolean;
  volume_label: string;
  disc_kind: DiscKind;
  state: string;
  make_mkv_index: number | null;
  last_seen: string | null;
};

export type Track = {
  source_id: number;
  disc_title_number?: number;
  name: string;
  duration_seconds: number;
  size_bytes: number;
  chapters: number;
  filename: string;
  selected: boolean;
  state: string;
};

export type Job = {
  id: string;
  drive_id: string;
  drive_letter: string;
  disc_label: string;
  disc_type: DiscKind;
  fingerprint: string;
  title: string;
  year: string;
  media_kind: MediaKind;
  state: string;
  stage: string;
  progress: number;
  status_detail: string;
  output_path: string;
  staging_path: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  recoverable: boolean;
  cancel_requested: boolean;
  version: number;
  metadata?: Record<string, unknown>;
  tracks?: Track[];
};

export type AiRepairSegment = {
  index: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  frame_count: number;
  ai_keyframe_count: number;
  missing_seconds?: number;
  damaged_frame_count?: number;
  preview: string;
  applied: boolean;
};

export type AiRepairSkipped = {
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  missing_seconds: number;
  reason: string;
};

export type AiRepairPlan = {
  estimate_id: string;
  // "estimated" comes from looking through a finished movie: priced, but not offered for approval yet.
  status: "estimated" | "awaiting_confirmation" | "applied" | "declined";
  summary?: string;
  notes?: string[];
  can_reread?: boolean;
  skipped?: AiRepairSkipped[];
  damaged_seconds?: number;
  mode: "openai_frame_bridge";
  model: string;
  quality: string;
  frame_count: number;
  ai_keyframe_count: number;
  estimated_max_cost_usd: number;
  actual_cost_usd?: number;
  estimate_is_ceiling: boolean;
  configured_cost_limit_usd: number;
  disclaimer: string;
  segments: AiRepairSegment[];
};

export type DamageTreatment = "loading_screen" | "skipped" | "brief_glitch" | "ai_frames";

export type DamageMoment = {
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  missing_seconds?: number;
  treatment?: DamageTreatment;
};

export type RescueStatus = {
  mode?: string;
  engine?: string;
  phase?: "starting" | "sweep" | "structures" | "retry" | "trim" | "scrape" | "done";
  retry_round?: number;
  stop_reason?: "" | "finished" | "budget" | "little_left_to_gain" | "skipped" | "structures";
  // "movie": only the movie and the disc's navigation are read; extras stay unread.
  scope?: "movie" | "disc";
  not_needed_bytes?: number;
  drive_faults?: number;
  total_bytes?: number;
  rescued_bytes?: number;
  unreadable_bytes?: number;
  pending_bytes?: number;
  movie_unreadable_bytes?: number;
  movie_pending_bytes?: number;
  deferred_bytes?: number;
  position_bytes?: number;
  read_errors?: number;
  damaged_areas?: number;
  in_damaged_zone?: boolean;
  rate_bytes_per_second?: number;
  elapsed_seconds?: number;
  extra_elapsed_seconds?: number;
  extra_budget_seconds?: number;
  finished_early?: boolean;
  budget_exhausted?: boolean;
  quality_warning?: string;
  updated_at?: string;
};

export function rescueFromJob(job: Job): RescueStatus | null {
  const value = job.metadata?.recovery;
  if (!value || typeof value !== "object" || !("total_bytes" in value)) return null;
  return value as RescueStatus;
}

export function aiRepairFromJob(job: Job): AiRepairPlan | null {
  const value = job.metadata?.ai_repair;
  if (!value || typeof value !== "object" || !("estimate_id" in value)) return null;
  return value as unknown as AiRepairPlan;
}

export type DamageDetails = {
  // The movie as read from the disc is kept next to the one with loading screens or AI frames.
  keptCopy: boolean;
  loadingScreenMethod: "splice" | "reencode" | "";
  // The finished movie waits for the choice: keep it as read, add loading screens, or replace frames with AI.
  choicePending: boolean;
  // The whole movie was decoded to find broken parts, not only the moments the disc never delivered.
  reviewed: boolean;
};

export function damageDetailsFromJob(job: Job): DamageDetails {
  const damage = job.metadata?.damage;
  const empty = { keptCopy: false, loadingScreenMethod: "" as const, choicePending: false, reviewed: false };
  if (!damage || typeof damage !== "object") return empty;
  const record = damage as Record<string, unknown>;
  const method = record.loading_screen_method;
  return {
    keptCopy: record.kept_copy === true,
    loadingScreenMethod: method === "splice" || method === "reencode" ? method : "",
    choicePending: record.choice === "pending",
    reviewed: typeof record.reviewed_at === "string",
  };
}

/** Whether a finished movie in the library can be looked through for broken parts. */
export function offerDamageReview(job: Job): boolean {
  return (
    job.state === "completed"
    && Boolean(job.output_path)
    && job.media_kind !== "music"
    && ["dvd", "bluray"].includes(job.disc_type)
    && !damageDetailsFromJob(job).reviewed
  );
}

/** What a data disc holds, listed before it is backed up. */
export type DiscContents = {
  kind: "game" | "software" | "media" | "pictures" | "documents" | "files";
  summary: string;
  markers?: string[];
  suggested_title?: string;
  file_count: number;
  total_bytes: number;
  truncated?: boolean;
  unreadable?: boolean;
  note?: string;
  top_level?: { name: string; file_count: number; total_bytes: number }[];
  entries?: { path: string; size: number }[];
};

export function discContentsFromJob(job: Job): DiscContents | null {
  const value = job.metadata?.disc_contents;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.file_count !== "number") return null;
  return record as unknown as DiscContents;
}

/** Whether this disc is waiting to be named before DiscDock backs it up. */
export function awaitingDiscBackup(job: Job): boolean {
  return job.state === "awaiting_input" && (job.disc_type === "data" || job.media_kind === "other");
}

export type AddToExisting = {
  // The library folder of the disc completed earlier, and the titles it already holds.
  outputPath: string;
  rippedTitles: number[];
};

export function addToExistingFromJob(job: Job): AddToExisting | null {
  const value = job.metadata?.add_to_existing;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.output_path !== "string" || !record.output_path) return null;
  const ripped = Array.isArray(record.ripped_titles) ? record.ripped_titles.filter((id): id is number => typeof id === "number") : [];
  return { outputPath: record.output_path, rippedTitles: ripped };
}

export type MusicRelease = {
  id: string;
  // As cyanrip lists it, for example "Into the Great Wide Open (BIEM / MCPS) (XE) (1991)".
  title: string;
};

// The releases of an audio CD to choose from, when MusicBrainz knows several.
export function musicReleasesFromJob(job: Job): MusicRelease[] {
  const value = job.metadata?.musicbrainz_releases;
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is MusicRelease => Boolean(item) && typeof item === "object" && typeof item.id === "string" && typeof item.title === "string");
}

export type AlbumRelease = {
  id: string;
  title: string;
  artist: string;
  date: string;
  country: string;
  label: string;
  disambiguation: string;
  format: string;
  track_count: number;
  artist_id?: string;
  disc_number?: number;
  disc_count?: number;
  // No release was chosen, so DiscDock used the first of this many.
  picked_first_of?: number;
  tagged_tracks?: number;
  // "manual": typed in the dashboard, with track names read from a photo of the case.
  source?: string;
  // The track names of an album entered in the dashboard, "" for a track left without a name.
  tracks?: { position: number; title: string }[];
};

export type ManualAlbum = {
  artist: string;
  title: string;
  year: string;
  // One name per track of the CD, "" for a track without a name.
  tracks: string[];
};

export type AlbumDetails = {
  album: AlbumRelease | null;
  candidates: AlbumRelease[];
  // For example "busy" when MusicBrainz declined with 503, or "several" while a release can be chosen.
  status: string;
  message: string;
  // DiscDock has read the CD's DiscID, so MusicBrainz is being asked or has been asked.
  lookedUp: boolean;
  // The number of tracks on the CD, once cyanrip has read it; 0 before.
  trackCount: number;
  // The CD's MusicBrainz DiscID, once cyanrip has read it; "" before.
  discid: string;
  // The tracks wait in staging for the album when the rip is done, instead of finishing without it.
  keepInStaging: boolean;
};

function isAlbumRelease(value: unknown): value is AlbumRelease {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string" && typeof record.title === "string";
}

export function albumFromJob(job: Job): AlbumDetails {
  const metadata = job.metadata ?? {};
  const lookup = metadata.album_lookup && typeof metadata.album_lookup === "object" ? metadata.album_lookup as Record<string, unknown> : {};
  const cd = metadata.cd && typeof metadata.cd === "object" ? metadata.cd as Record<string, unknown> : {};
  return {
    album: isAlbumRelease(metadata.album) ? metadata.album : null,
    candidates: Array.isArray(metadata.album_candidates) ? metadata.album_candidates.filter(isAlbumRelease) : [],
    status: typeof lookup.status === "string" ? lookup.status : "",
    message: typeof lookup.message === "string" ? lookup.message : "",
    lookedUp: Boolean(metadata.cd),
    trackCount: typeof cd.tracks === "number" ? cd.tracks : 0,
    discid: typeof cd.discid === "string" ? cd.discid : "",
    keepInStaging: metadata.album_hold === true,
  };
}

export function albumName(release: AlbumRelease): string {
  return release.artist ? `${release.artist} — ${release.title}` : release.title;
}

// "front_original" is the photo of the front as taken, before it was edited into the cover.
export type AlbumPhotoSide = "front" | "front_original" | "back";

const ALBUM_PHOTO_KEYS: Record<AlbumPhotoSide, string> = { front: "album_cover", front_original: "album_cover_original", back: "album_back_photo" };

// A photo of the CD's case taken in the dashboard, or "" when none is saved. Each new photo gets a new URL.
export function albumPhotoUrl(job: Job, side: AlbumPhotoSide): string {
  const value = job.metadata?.[ALBUM_PHOTO_KEYS[side]];
  const photo = value && typeof value === "object" ? value as Record<string, unknown> : {};
  if (typeof photo.file !== "string" || !photo.file) return "";
  const added = typeof photo.added_at === "string" ? photo.added_at : String(job.version);
  return apiUrl(`/api/v1/jobs/${encodeURIComponent(job.id)}/album/photos/${side}?added=${encodeURIComponent(added)}`);
}

// The cover for a CD's job card: the photo of the front of the case, or else the front cover the Cover Art
// Archive has for the MusicBrainz release. Releases without a cover there simply fail to load.
export function albumCoverUrl(job: Job): string {
  return albumPhotoUrl(job, "front") || archiveCoverUrl(albumFromJob(job).album);
}

// The front cover the Cover Art Archive has for a MusicBrainz release; "" for an album entered by hand.
export function archiveCoverUrl(album: AlbumRelease | null): string {
  return album && album.source !== "manual" && /^[0-9a-f-]{36}$/.test(album.id) ? `https://coverartarchive.org/release/${album.id}/front-250` : "";
}

// "1991 · XE · MCA · CD · 10 tracks · BIEM / MCPS": what tells the releases of one album apart.
// withSource adds "entered by hand" for an album typed in the dashboard, where nothing else says so.
export function describeRelease(release: AlbumRelease, withSource = true): string {
  const tracks = release.track_count ? `${release.track_count} tracks` : "";
  const entered = withSource && release.source === "manual" ? "entered by hand" : "";
  return [release.date, release.country, release.label, release.format, tracks, release.disambiguation, entered].filter(Boolean).join(" · ");
}

export function damageMomentsFromJob(job: Job): DamageMoment[] {
  const damage = job.metadata?.damage;
  if (damage && typeof damage === "object" && "moments" in damage && Array.isArray(damage.moments)) {
    return (damage.moments as DamageMoment[]).filter((moment) => typeof moment?.start_seconds === "number");
  }
  // Movies finished before damage was recorded still have their AI analysis.
  const plan = aiRepairFromJob(job);
  if (!plan) return [];
  const untreated = (seconds: number): DamageTreatment => (seconds >= 2 ? "skipped" : "brief_glitch");
  const moments: DamageMoment[] = [
    ...plan.segments.map((segment) => ({ start_seconds: segment.start_seconds, end_seconds: segment.end_seconds, duration_seconds: segment.duration_seconds, treatment: plan.status === "applied" ? "ai_frames" as const : untreated(segment.duration_seconds) })),
    ...(plan.skipped ?? []).map((item) => ({ start_seconds: item.start_seconds, end_seconds: item.end_seconds, duration_seconds: item.duration_seconds, missing_seconds: item.missing_seconds, treatment: untreated(item.duration_seconds) })),
  ];
  return moments.sort((left, right) => left.start_seconds - right.start_seconds);
}

export type DiscDockNotification = {
  id: number;
  job_id: string | null;
  event_type: string;
  title: string;
  body: string;
  state: string;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  created_at: string;
};

export type Health = {
  ok: boolean;
  service: string;
  version: string;
  tools: Record<string, boolean>;
  drive_count: number;
  configured: boolean;
  automatic_ripping: boolean;
  drive_blockers?: string[];
};

export type Settings = {
  [key: string]: unknown;
  auto_rip: boolean;
  auto_eject: boolean;
  prevent_sleep: boolean;
  skip_transcode: boolean;
  keep_raw_after_transcode: boolean;
  main_feature: boolean;
  extras: boolean;
  always_choose_titles: boolean;
  min_length_seconds: number;
  max_length_seconds: number;
  duplicate_policy: string;
  metadata_provider: string;
  omdb_enabled: boolean;
  ai_repair_enabled: boolean;
  ai_repair_model: string;
  ai_repair_quality: string;
  ai_repair_keyframes_per_second: number;
  ai_repair_cost_limit_usd: number;
  rescue_extra_minutes: number;
  damaged_disc_action: string;
  damage_placeholder: string;
  cd_read_offset: number;
  notifications_enabled: boolean;
  make_mkv_path: string;
  handbrake_path: string;
  ffmpeg_path: string;
  ffprobe_path: string;
  vlc_path: string;
  cyanrip_path: string;
  rip_mode: string;
  data_root: string;
  directories: Record<string, string>;
  secrets: Record<string, boolean>;
};

export type Bootstrap = {
  health: Health;
  drives: Drive[];
  jobs: Job[];
  settings: Settings;
  notifications: DiscDockNotification[];
};

export function apiBase(): string {
  if (typeof window === "undefined") return "http://127.0.0.1:8199";
  return window.location.port === "5173" ? "http://127.0.0.1:8199" : "";
}

export function apiUrl(path: string): string {
  return `${apiBase()}${path}`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: string };
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function eventsUrl(): string {
  return apiUrl("/api/v1/events");
}
