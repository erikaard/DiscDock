"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Activity, AlertTriangle, Check, CircleStop, Crop, Disc3, Eject, Film, FolderOpen,
  HardDrive, ImageOff, KeyRound, ListChecks, Loader2, Music, PencilLine, Play, RefreshCw,
  LifeBuoy, MoreHorizontal, RotateCw, ScanBarcode, Search, Settings2, SkipForward, Sparkles, Wrench,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";
import { addToExistingFromJob, aiRepairFromJob, albumCoverUrl, albumFromJob, albumName, albumPhotoUrl, archiveCoverUrl, awaitingDiscBackup, damageDetailsFromJob, describeRelease, discContentsFromJob, musicReleasesFromJob, offerDamageReview, rescueFromJob, type AiRepairPlan, type AlbumRelease, type Bootstrap, type Drive, type Job, type MediaKind, type MetadataCandidate, type RescueStatus } from "@/lib/discdock-api";
import type { DiscDockControls } from "@/hooks/use-discdock";
import { clearAlbumDraft, clearCoverEdit, readAlbumDraft, readCoverEdit, writeAlbumDraft, writeCoverEdit, type AlbumDraft } from "@/lib/album-draft";
import type { CoverEdit } from "@/lib/cover-image";
import { readCaseText } from "@/lib/ocr";
import { tracksFromText } from "@/lib/ocr-tracks";
import { BarcodeScanner } from "./barcode-scanner";
import { CoverEditor } from "./cover-editor";
import { PhotoCapture } from "./photo-capture";
import { formatBytes, formatDate, StateBadge, titleFor } from "./status";

const ACTIVE = new Set(["detected", "inspecting", "identifying", "awaiting_input", "awaiting_repair", "queued", "ripping", "ripped", "verifying", "transcoding", "finalizing", "ejecting", "cancelling"]);
// The service refuses to eject the disc while its job is in one of these states.
const EJECT_REFUSED = new Set(["detected", "inspecting", "identifying", "awaiting_input", "queued", "ripping", "ripped", "verifying", "transcoding", "finalizing", "ejecting", "cancelling"]);
// DiscDock reads the disc, or is about to. Opening it in VLC then would slow the rip and can disturb decryption.
const DISC_READING = new Set(["detected", "inspecting", "identifying", "queued", "ripping", "ejecting", "cancelling"]);
// A CD in these states can still be told to wait in staging for its album when the rip is done.
const CD_BEFORE_TAGGING = new Set(["detected", "inspecting", "identifying", "queued", "ripping", "ripped", "verifying", "ejecting"]);
const STAGES = ["inspecting", "identifying", "ripping", "verifying", "finalizing"];
const STAGE_LABELS = ["Inspect", "Identify", "Rip", "Verify", "Finish"];

function formatPercent(value: number): string {
  const safe = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
  return `${safe.toFixed(2).padStart(5, "0")}%`;
}

function safePoster(value: unknown): string {
  return typeof value === "string" && value.startsWith("https://") ? value : "";
}

type ImageStatus = "none" | "loading" | "loaded" | "missing";

// Whether the image at a URL loads, so a cover that does not exist leaves no empty frame and can be added instead.
function useImageStatus(url: string): ImageStatus {
  const [result, setResult] = useState<{ url: string; loaded: boolean } | null>(null);
  useEffect(() => {
    if (!url) return;
    let current = true;
    const image = new Image();
    image.onload = () => { if (current) setResult({ url, loaded: true }); };
    image.onerror = () => { if (current) setResult({ url, loaded: false }); };
    image.src = url;
    return () => { current = false; image.onload = null; image.onerror = null; };
  }, [url]);
  if (!url) return "none";
  if (result?.url !== url) return "loading";
  return result.loaded ? "loaded" : "missing";
}

// The URL once its image has loaded, or "".
function useLoadedImage(url: string): string {
  return useImageStatus(url) === "loaded" ? url : "";
}

function candidateFromJob(job: Job): MetadataCandidate | null {
  const provider = String(job.metadata?.provider ?? "");
  if (!provider) return null;
  const rawKind = String(job.metadata?.media_kind ?? job.media_kind);
  const mediaKind: MediaKind = ["movie", "series", "music", "data", "unknown"].includes(rawKind) ? rawKind as MediaKind : job.media_kind;
  return {
    provider,
    provider_id: String(job.metadata?.provider_id ?? ""),
    title: String(job.metadata?.title ?? job.title),
    year: String(job.metadata?.year ?? job.year),
    media_kind: mediaKind,
    poster_url: safePoster(job.metadata?.poster_url),
    plot: String(job.metadata?.plot ?? ""),
    runtime_minutes: Number(job.metadata?.runtime_minutes ?? 0),
    user_selected: Boolean(job.metadata?.user_selected),
  };
}

function discReadWarning(job: Job): string {
  const warnings = job.metadata?.warnings;
  if (!Array.isArray(warnings)) return "";
  const warning = warnings.find((item) => item && typeof item === "object" && "code" in item && item.code === "disc_read_error");
  return warning && typeof warning === "object" && "message" in warning ? String(warning.message) : "";
}

function damageRecoveryActive(job: Job): boolean {
  return Boolean(job.metadata?.recovery_requested) || (
    ["detected", "queued", "ripping", "verifying", "transcoding", "finalizing"].includes(job.state)
    && job.stage === "recovering"
  );
}

function aiRepairActive(job: Job): boolean {
  return Boolean(job.metadata?.ai_repair_requested) || ["ai_salvage", "ai_analyzing", "ai_repair"].includes(job.stage);
}

function offerDamageRecovery(job: Job): boolean {
  return ["dvd", "bluray"].includes(job.disc_type) && ["ripping", "failed", "cancelled", "interrupted"].includes(job.state) && Boolean(discReadWarning(job)) && !damageRecoveryActive(job);
}

/** The free estimate for replacing a finished movie's broken parts, when one was worked out. */
function aiEstimateFromJob(job: Job): AiRepairPlan | null {
  const plan = aiRepairFromJob(job);
  return plan && plan.status !== "applied" && plan.segments.length > 0 ? plan : null;
}

function offerAiRepair(job: Job): boolean {
  return ["ripping", "failed", "cancelled", "interrupted"].includes(job.state) && Boolean(discReadWarning(job)) && !damageRecoveryActive(job) && !aiRepairFromJob(job) && !aiRepairActive(job);
}

function offerFinishRescued(job: Job): boolean {
  // Once the whole disc was read, a rescue stopped by a stuck drive can still finish from its saved image.
  const phase = rescueFromJob(job)?.phase ?? "";
  return ["failed", "cancelled", "interrupted"].includes(job.state) && ["structures", "retry", "trim", "scrape", "done"].includes(phase);
}

function confirmFinishRescued(job: Job, controls: DiscDockControls): void {
  const rescue = rescueFromJob(job);
  const missing = rescue?.movie_pending_bytes ?? rescue?.pending_bytes ?? 0;
  const route = job.metadata?.recovery_route;
  const aiRoute = (route && typeof route === "object" && "mode" in route && route.mode === "ai_repair") || ["ai_repair_not_possible", "ai_repair_preparation_failed"].includes(job.error_code ?? "");
  const next = aiRoute ? "The AI review comes next; no API credit is used until you approve it." : "The movie then goes to your library.";
  if (window.confirm(`Finish the movie from what has been rescued, without reading the disc again? ${formatSize(missing)} is still unread, so the movie skips briefly there. ${next}`)) void controls.finishRescue(job.id);
}

/** A disc DiscDock completed before, waiting for the choice to rip it again or add titles to its folder. */
function isCompletedDuplicate(job: Job): boolean {
  return job.state === "blocked" && job.error_code === "duplicate";
}

function retryLabel(job: Job): string {
  const route = job.metadata?.recovery_route;
  const mode = route && typeof route === "object" && "mode" in route ? String(route.mode) : "";
  if (isCompletedDuplicate(job)) return "Rip again as a new copy";
  if (job.error_code === "ai_repair_failed") return "Review AI repair again";
  if (mode === "ai_repair" || job.error_code === "ai_repair_preparation_failed" || job.error_code === "ai_repair_not_possible") return "Retry AI analysis";
  if (["vlc_best_effort", "sector_best_effort"].includes(mode) || (job.metadata?.recovery && typeof job.metadata.recovery === "object")) return "Continue best effort";
  return "Retry";
}

function confirmDamageRecovery(job: Job, controls: DiscDockControls): void {
  const opening = job.error_code === "disc_unreadable"
    ? "MakeMKV could not open this disc. DiscDock first reads the disc's file system and navigation data so MakeMKV can find the movie, then reads the movie itself,"
    : "DiscDock reads the movie directly from the disc,";
  const confirmed = window.confirm(
    `${job.state === "ripping" ? "Stop MakeMKV and start" : "Start"} best-effort recovery? ${opening} skips past spots the drive cannot read, retries them until little more comes back (you can skip that at any time), and then finishes the movie. It may briefly freeze or show blocky pictures where the disc is damaged; unreadable frames cannot be restored. A rescue that has already started continues where it stopped.`
  );
  if (confirmed) void controls.recoverDamaged(job.id);
}

function formatRepairTime(seconds: number): string {
  const safe = Math.max(0, seconds);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = (safe % 60).toFixed(3).padStart(6, "0");
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${remainder}`;
}

function PosterArt({ url, title, compact = false, square = false }: { url: string; title: string; compact?: boolean; square?: boolean }) {
  const size = square ? "size-[6.7rem] rounded-xl" : compact ? "h-20 w-[3.35rem] rounded-lg" : "h-40 w-[6.7rem] rounded-xl";
  return (
    <div
      role="img"
      aria-label={url ? `${title} cover art` : `No cover art for ${title}`}
      className={`${size} poster-reveal grid shrink-0 place-items-center overflow-hidden border border-white/10 bg-[#101d20] bg-cover bg-center shadow-[0_14px_35px_rgb(0_0_0/28%)]`}
      style={url ? { backgroundImage: `url("${url.replaceAll('"', "%22")}")` } : undefined}
    >
      {!url && <ImageOff className="size-5 text-muted-foreground/60" />}
    </div>
  );
}

export function DashboardView({ data, loading, error, busy, controls, openSettings }: {
  data: Bootstrap | null;
  loading: boolean;
  error: string | null;
  busy: string | null;
  controls: DiscDockControls;
  openSettings: () => void;
}) {
  if (loading && !data) return <DashboardSkeleton />;
  const drives: Array<Drive | undefined> = data?.drives.length ? data.drives : [undefined];
  const recent = data?.jobs.filter((job) => !ACTIVE.has(job.state)).slice(0, 4) ?? [];

  return (
    <div className="space-y-6">
      {error && (
        <Alert className="border-red-400/20 bg-red-400/8 text-red-100">
          <AlertTriangle className="size-4" />
          <AlertTitle>Windows service unavailable</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {data?.health.drive_blockers?.map((blocker) => <Alert key={blocker} className="border-amber-400/20 bg-amber-400/8 text-amber-100"><AlertTriangle className="size-4" /><AlertTitle>Drive is in use</AlertTitle><AlertDescription>{blocker}. Close that app before starting a rip.</AlertDescription></Alert>)}
      {drives.map((drive, index) => {
        // A reconnected USB drive can get a new id; jobs of a drive that is gone show on the first drive.
        const belongsHere = (job: Job) => job.drive_id === drive?.id || (index === 0 && !data?.drives.some((item) => item.id === job.drive_id));
        const activeJob = data?.jobs.find((job) => ACTIVE.has(job.state) && belongsHere(job));
        const discJob = activeJob ?? data?.jobs.find((job) => belongsHere(job) && job.disc_label === drive?.volume_label);
        return <div key={drive?.id ?? `missing-${index}`} className="grid gap-6 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,.85fr)]"><DriveCard drive={drive} job={discJob} busy={busy} controls={controls} automation={Boolean(data?.settings.auto_rip)} /><ActiveJobCard key={activeJob?.id ?? `idle-${drive?.id ?? index}`} job={activeJob} busy={busy} controls={controls} /></div>;
      })}
      <WaitingCds jobs={data?.jobs ?? []} busy={busy} controls={controls} />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,.85fr)]">
        <RecentJobs jobs={recent} busy={busy} controls={controls} />
        <SystemHealth data={data} openSettings={openSettings} />
      </div>
    </div>
  );
}

const DISC_TYPES = [
  { kind: "bluray", label: "Blu-ray" },
  { kind: "dvd", label: "DVD" },
  { kind: "audio_cd", label: "CD" },
];

function discKindLabel(kind: string): string {
  return ({ bluray: "Blu-ray", dvd: "DVD", audio_cd: "Audio CD", data: "Data disc" } as Record<string, string>)[kind] ?? "Optical media";
}

function DiscTypeBadge({ drive, hasDisc }: { drive?: Drive; hasDisc: boolean }) {
  // The type of the inserted disc lights up; with no disc all supported types stay muted.
  const inserted = hasDisc ? drive?.disc_kind : undefined;
  const known = DISC_TYPES.some((type) => type.kind === inserted);
  return (
    <div role="group" aria-label={known && inserted ? `${discKindLabel(inserted)} inserted` : "Supports Blu-ray, DVD and CD"} className="inline-flex items-center gap-0.5 rounded-full border border-white/10 p-0.5 text-xs">
      {DISC_TYPES.map((type) => {
        const active = inserted === type.kind;
        return <span key={type.kind} aria-current={active ? "true" : undefined} className={`rounded-full px-2 py-0.5 font-medium transition-colors ${active ? "bg-primary text-primary-foreground shadow-[0_0_14px_rgb(64_221_198/35%)]" : "text-muted-foreground"}`}>{type.label}</span>;
      })}
    </div>
  );
}

function DriveCard({ drive, job, busy, controls, automation }: { drive?: Drive; job?: Job; busy: string | null; controls: DiscDockControls; automation: boolean }) {
  const hasDisc = Boolean(drive?.media_loaded);
  const titleIdentified = Boolean(job?.title && !["detected", "inspecting", "identifying"].includes(job.state));
  const title = !drive ? "No optical drive found" : hasDisc ? titleIdentified ? titleFor(job as Job) : drive.volume_label || "Disc loaded" : "Ready for a disc";
  const isRipping = job?.state === "ripping";
  const ejectRefused = Boolean(job && EJECT_REFUSED.has(job.state));
  const readingDisc = Boolean(job && DISC_READING.has(job.state));
  const detail = !drive
    ? "Connect an optical drive and refresh. DiscDock will detect it without WSL or Docker."
    : hasDisc
      ? `${discKindLabel(drive.disc_kind)} is ready to inspect on ${drive.letter}.`
      : `Insert a disc into ${drive.letter}. Windows will keep direct control of the reader.`;
  return (
    <Card className="overflow-hidden border-white/8 bg-card shadow-none">
      <CardContent className="grid min-h-[330px] gap-8 p-6 sm:p-8 md:grid-cols-[230px_minmax(0,1fr)] md:items-center">
        <div className="relative mx-auto grid size-[210px] place-items-center" aria-hidden="true">
          <div className={`disc-visual absolute inset-0 rounded-full transition-opacity ${drive ? "opacity-100" : "opacity-35"} ${isRipping ? "disc-ripping" : ""}`} />
          <div className="absolute inset-[18px] rounded-full border border-white/8" />
          <div className="absolute inset-[42px] rounded-full border border-white/6" />
          <div className={`z-10 grid size-[78px] place-items-center rounded-full border border-primary/25 bg-[#0c181b] shadow-[0_0_35px_rgb(64_221_198/12%)] ${isRipping ? "disc-center-ripping" : ""}`}>
            {hasDisc ? <Disc3 className="size-7 text-primary" strokeWidth={1.7} /> : <HardDrive className="size-7 text-primary" strokeWidth={1.7} />}
          </div>
        </div>
        <div className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <Badge className="border-primary/25 bg-primary/10 text-primary hover:bg-primary/10">{drive?.letter ? `Drive ${drive.letter}` : "Drive offline"}</Badge>
            <DiscTypeBadge drive={drive} hasDisc={hasDisc} />
            {automation && <Badge variant="outline" className="border-sky-400/20 bg-sky-400/8 text-sky-200">Auto mode</Badge>}
          </div>
          <p className="truncate text-sm text-muted-foreground">{drive?.name ?? "Windows is not reporting a reader"}</p>
          <h2 className="mt-1 text-2xl font-semibold tracking-tight">{title}</h2>
          <p className="mt-3 max-w-xl text-sm leading-6 text-muted-foreground">{detail}</p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Button disabled={!drive || !hasDisc || busy !== null} className="gap-2 bg-primary text-primary-foreground hover:bg-primary/90" onClick={() => drive && void controls.scan(drive.id)}>
              <RotateCw className={busy === "scan" ? "animate-spin" : ""} /> {busy === "scan" ? "Starting…" : "Start rip"}
            </Button>
            <Button disabled={!drive || !hasDisc || !["dvd", "bluray"].includes(drive.disc_kind) || busy !== null} title={drive && ["dvd", "bluray"].includes(drive.disc_kind) ? "Copy the disc directly, skipping past unreadable spots, then extract the movie" : "Rescue ripping works with DVDs and Blu-rays"} variant="outline" className="gap-2 border-amber-400/20 bg-amber-400/[0.035] text-amber-100 hover:bg-amber-400/10" onClick={() => drive && void controls.scan(drive.id, false, "sector_rescue")}>
              <AlertTriangle className={busy === "scan-rescue" ? "animate-pulse" : ""} /> {busy === "scan-rescue" ? "Starting rescue…" : "Start rescue rip"}
            </Button>
            <Button disabled={!drive || !hasDisc || busy !== null} variant="outline" className="gap-2 border-white/10 bg-white/[0.025] hover:bg-white/5" onClick={() => drive && void controls.scan(drive.id, true)}><ListChecks /> Choose titles</Button>
            <span className="inline-flex" title={ejectRefused ? "Stop the job before ejecting the disc" : undefined}><Button disabled={!drive || ejectRefused || busy !== null} variant="outline" className="gap-2 border-white/10 bg-white/[0.025] hover:bg-white/5" onClick={() => drive && void controls.eject(drive.id)}><Eject /> Eject</Button></span>
            <span className="inline-flex" title={readingDisc ? "DiscDock is reading the disc. Preview it when the rip is done or stopped." : "Open the disc in VLC"}><Button disabled={!drive || !hasDisc || readingDisc || busy !== null} variant="ghost" className="gap-2 text-muted-foreground" onClick={() => drive && void controls.preview(drive.id)}><Play /> Preview</Button></span>
            <Button disabled={busy !== null} variant="ghost" size="icon" aria-label="Refresh drives" onClick={() => void controls.refresh()}><RefreshCw className={busy === "refresh" ? "animate-spin" : ""} /></Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ActiveJobCard({ job, busy, controls }: { job?: Job; busy: string | null; controls: DiscDockControls }) {
  const [editingTitle, setEditingTitle] = useState(false);
  const albumCover = useLoadedImage(job?.disc_type === "audio_cd" ? albumCoverUrl(job) : "");
  if (!job) {
    return (
      <Card className="border-white/8 bg-card shadow-none">
        <CardContent className="flex min-h-[330px] flex-col items-center justify-center px-8 text-center">
          <div className="grid size-12 place-items-center rounded-2xl bg-primary/8 text-primary"><Activity className="size-5" /></div>
          <h2 className="mt-5 text-lg font-semibold">No active job</h2>
          <p className="mt-2 max-w-xs text-sm leading-6 text-muted-foreground">A live progress card will appear here as soon as a disc scan begins.</p>
        </CardContent>
      </Card>
    );
  }
  if (job.state === "awaiting_input" && musicReleasesFromJob(job).length > 0) return <ReleaseChoiceCard key={`${job.id}-${job.version}`} job={job} busy={busy} controls={controls} />;
  if (awaitingDiscBackup(job)) return <DiscBackupCard key={`${job.id}-${job.version}`} job={job} busy={busy} controls={controls} />;
  if (job.state === "awaiting_input") return <ManualSelectionCard key={`${job.id}-${job.version}`} job={job} busy={busy} controls={controls} />;
  const stageIndex = ["recovering", "ai_salvage", "ai_analyzing", "ai_review", "ai_repair"].includes(job.stage) ? 2 : Math.max(0, STAGES.indexOf(job.stage));
  const isCd = job.disc_type === "audio_cd";
  const poster = isCd ? albumCover : safePoster(job.metadata?.poster_url);
  const progress = formatPercent(job.progress);
  const readWarning = discReadWarning(job);
  const aiRepair = aiRepairFromJob(job);
  const rescue = rescueFromJob(job);
  const showRescue = Boolean(rescue) && ["recovering", "ai_salvage"].includes(job.stage) && ACTIVE.has(job.state);
  return (
    <Card className="job-reveal border-white/8 bg-card shadow-none">
      <CardHeader className="pb-3">
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
          <div className="min-w-0"><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Active job</p><CardTitle className="mt-2 break-words text-xl leading-snug">{titleFor(job)}</CardTitle></div>
          <StateBadge state={job.state} />
        </div>
      </CardHeader>
      <CardContent>
        {rescue && showRescue && <RescuePanel job={job} rescue={rescue} busy={busy} controls={controls} />}
        {readWarning && !aiRepair && !showRescue && <div className="mb-5 rounded-xl border border-amber-400/25 bg-amber-400/9 p-4 text-amber-100"><div className="flex gap-3"><AlertTriangle className="mt-0.5 size-5 shrink-0" /><div className="min-w-0 flex-1"><p className="text-sm font-semibold">{damageRecoveryActive(job) ? "Best-effort recovery active" : aiRepairActive(job) ? "Preparing AI repair estimate" : "Disc read problem"}</p><p className="mt-1 text-xs leading-5 text-amber-100/80">{damageRecoveryActive(job) ? "DiscDock is copying the disc directly. Where the drive cannot read a block, it skips to the next moment of video and retries the skipped spots afterwards." : aiRepairActive(job) ? "DiscDock is recovering the readable video and finding the damaged frames. Nothing is sent to OpenAI in this step." : readWarning}</p></div></div>{!damageRecoveryActive(job) && !aiRepairActive(job) && <div className="mt-4 flex flex-col gap-2"><Button disabled={busy !== null || !offerDamageRecovery(job)} variant="outline" size="sm" className="h-auto min-h-9 w-full whitespace-normal border-amber-300/25 bg-amber-100/5 px-3 py-2 text-center leading-4 text-amber-50 hover:bg-amber-100/10" title="Keep readable video and skip damaged spots" onClick={() => confirmDamageRecovery(job, controls)}>{busy === "recover-damaged" ? <Loader2 className="size-3.5 shrink-0 animate-spin" /> : <Play className="size-3.5 shrink-0" />} Continue with best effort</Button><Button disabled={busy !== null || !offerAiRepair(job)} size="sm" className="h-auto min-h-9 w-full whitespace-normal bg-primary px-3 py-2 text-center leading-4 text-primary-foreground" onClick={() => void controls.prepareAiRepair(job.id)}>{busy === "prepare-ai-repair" ? <Loader2 className="size-3.5 shrink-0 animate-spin" /> : <Sparkles className="size-3.5 shrink-0" />} Analyze AI repair · free</Button></div>}</div>}
        {aiRepair?.status === "awaiting_confirmation" && <AiRepairEstimateCard job={job} plan={aiRepair} busy={busy} controls={controls} />}
        {isCd && <AlbumStatus job={job} busy={busy} controls={controls} />}
        <div className={poster ? "grid grid-cols-[6.7rem_minmax(0,1fr)] gap-5" : ""}>
          {poster && <PosterArt square={isCd} url={poster} title={titleFor(job)} />}
          <div className="min-w-0">
            <div className="flex items-end justify-between gap-4">
              <div><p className="text-sm font-medium">{job.status_detail || "Working"}</p><p className="mt-1 text-xs text-muted-foreground">{job.drive_letter} · started {formatDate(job.created_at)}</p></div>
            </div>
            <div className={`relative mt-4 ${ACTIVE.has(job.state) ? "progress-live" : ""}`} aria-label={`${progress} complete`}>
              <Progress value={job.progress} className="h-7 bg-white/8 [&>div]:bg-primary" />
              <span className="pointer-events-none absolute inset-0 grid place-items-center font-mono text-xs font-bold tracking-wide text-white drop-shadow-[0_1px_2px_rgb(0_0_0/75%)]">{progress}</span>
            </div>
            <div className="mt-5 grid grid-cols-5 gap-1.5">
              {STAGE_LABELS.map((label, index) => <div key={label} className="min-w-0"><div className={`mb-2 h-1 rounded-full ${index < stageIndex ? "bg-emerald-400" : index === stageIndex ? "stage-active bg-primary" : "bg-white/8"}`} /><p className="truncate text-[11px] text-muted-foreground">{label}</p></div>)}
            </div>
            {job.error_message && <p className="mt-5 rounded-lg border border-amber-400/15 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{job.error_message}</p>}
            <div className="mt-6 flex flex-wrap gap-2">
              {job.output_path && <Button variant="outline" size="sm" className="gap-2 border-white/10 bg-transparent" onClick={() => void controls.openOutput(job.id)}><FolderOpen className="size-3.5" /> Open output</Button>}
              <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10 bg-transparent" onClick={() => setEditingTitle((current) => !current)}><Search className="size-3.5" /> {isCd ? (editingTitle ? "Close" : "Find the album") : editingTitle ? "Close title search" : "Search or rename"}</Button>
              <Button disabled={busy !== null} variant="ghost" size="sm" className="gap-2 text-muted-foreground hover:text-red-300" onClick={() => void controls.cancel(job.id)}><CircleStop className="size-3.5" /> Stop safely</Button>
            </div>
          </div>
        </div>
        {editingTitle && (isCd ? <AlbumFinder job={job} busy={busy} controls={controls} onChosen={() => setEditingTitle(false)} /> : <WorkingTitleEditor job={job} busy={busy} controls={controls} onSaved={() => setEditingTitle(false)} />)}
      </CardContent>
    </Card>
  );
}

function AiRepairEstimateCard({ job, plan, busy, controls }: { job: Job; plan: AiRepairPlan; busy: string | null; controls: DiscDockControls }) {
  const withinLimit = plan.estimated_max_cost_usd <= plan.configured_cost_limit_usd;
  const skipped = plan.skipped ?? [];
  const canGenerate = plan.segments.length > 0;
  const approve = () => {
    const skippedNote = skipped.length ? ` ${skipped.length} longer damaged ${skipped.length === 1 ? "stretch stays" : "stretches stay"} skipped.` : "";
    const confirmed = window.confirm(`Generate ${plan.frame_count} replacement frames with OpenAI? This costs at most $${plan.estimated_max_cost_usd.toFixed(2)}. The frames are generated, not the original picture.${skippedNote}`);
    if (confirmed) void controls.applyAiRepair(job.id, plan.estimate_id, plan.estimated_max_cost_usd);
  };
  const keep = () => {
    const confirmed = window.confirm("Keep the movie without AI frames? It plays straight through, with brief freezes or jumps where the disc is damaged. No API credit is used.");
    if (confirmed) void controls.keepWithoutAi(job.id);
  };
  return (
    <div className="mb-5 rounded-xl border border-primary/25 bg-primary/7 p-4">
      <div className="flex items-start gap-3"><Sparkles className="mt-0.5 size-5 shrink-0 text-primary" /><div className="min-w-0 flex-1"><p className="text-sm font-semibold">{canGenerate ? "AI reconstruction estimate" : "Damage review"}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{plan.summary ?? `OpenAI draws ${plan.ai_keyframe_count} keyframes and DiscDock fills in the ${plan.frame_count} frames between them.`} No paid request has been made.</p></div></div>
      {canGenerate && <div className="mt-4 grid grid-cols-3 gap-2 text-center"><div className="rounded-lg bg-black/15 px-2 py-3"><p className="text-lg font-semibold">{plan.frame_count}</p><p className="text-[10px] uppercase tracking-wide text-muted-foreground">frames used</p></div><div className="rounded-lg bg-black/15 px-2 py-3"><p className="text-lg font-semibold">{plan.ai_keyframe_count}</p><p className="text-[10px] uppercase tracking-wide text-muted-foreground">API images</p></div><div className="rounded-lg bg-black/15 px-2 py-3"><p className="text-lg font-semibold">${plan.estimated_max_cost_usd.toFixed(2)}</p><p className="text-[10px] uppercase tracking-wide text-muted-foreground">max cost</p></div></div>}
      {canGenerate && <div className="mt-3 space-y-1.5">{plan.segments.map((segment) => <div key={segment.index} className="flex items-center justify-between gap-3 rounded-lg border border-white/7 px-3 py-2 text-xs"><span>AI fills damage {segment.index}</span><span className="font-mono text-muted-foreground">{formatRepairTime(segment.start_seconds)} – {formatRepairTime(segment.end_seconds)}</span></div>)}</div>}
      {skipped.length > 0 && <div className="mt-3 space-y-1.5">{skipped.slice(0, 6).map((item) => <div key={`${item.start_seconds}-${item.end_seconds}`} className="rounded-lg border border-white/7 px-3 py-2 text-xs"><div className="flex items-center justify-between gap-3"><span>Stays skipped · {item.duration_seconds.toFixed(1)} s</span><span className="font-mono text-muted-foreground">{formatRepairTime(item.start_seconds)} – {formatRepairTime(item.end_seconds)}</span></div><p className="mt-1 text-[11px] leading-4 text-muted-foreground">Damage {item.reason}.</p></div>)}{skipped.length > 6 && <p className="px-1 text-[11px] text-muted-foreground">…and {skipped.length - 6} more skipped {skipped.length - 6 === 1 ? "moment" : "moments"}</p>}</div>}
      {plan.notes?.map((note) => <p key={note} className="mt-3 text-xs leading-5 text-muted-foreground">{note}</p>)}
      {canGenerate && <p className="mt-3 rounded-lg border border-amber-400/15 bg-amber-400/7 px-3 py-2 text-xs leading-5 text-amber-100">Generated frames, not the original footage. Audio stays unchanged and may drop out briefly.</p>}
      {canGenerate && !withinLimit && <p className="mt-3 text-xs text-red-300">This estimate exceeds your ${plan.configured_cost_limit_usd.toFixed(2)} Settings limit. Raise that limit before approving.</p>}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        {plan.can_reread && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-amber-400/20 bg-transparent text-amber-100" title="Put the disc back in the drive first" onClick={() => void controls.retry(job.id)}>{busy === "retry" ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />} Read skipped spots again</Button>}
        <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10 bg-transparent" onClick={keep}>{busy === "keep-without-ai" ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Keep movie without AI</Button>
        {canGenerate && <Button disabled={busy !== null || !withinLimit} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={approve}>{busy === "apply-ai-repair" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />} Generate & use {plan.frame_count} frames · up to ${plan.estimated_max_cost_usd.toFixed(2)}</Button>}
      </div>
    </div>
  );
}

function formatSize(bytes: number | undefined): string {
  const value = Math.max(0, Number(bytes ?? 0));
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(value >= 100 * 1024 ** 2 ? 0 : 1)} MB`;
  return `${Math.round(value / 1024)} KB`;
}

const RESCUE_PHASES: Record<string, string> = {
  starting: "Preparing the rescue",
  sweep: "Pass 1 of 2 · copying the disc and skipping past unreadable spots",
  structures: "Reading the disc's file system and navigation data",
  retry: "Pass 2 of 2 · retrying the skipped spots",
  trim: "Pass 2 of 2 · retrying the skipped spots",
  scrape: "Pass 2 of 2 · retrying the skipped spots",
  done: "Rescue image ready · extracting the movie",
};

const RETRY_PHASES = ["structures", "retry", "trim", "scrape"];

function RescueStat({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg bg-black/15 px-2 py-2.5"><p className="text-sm font-semibold tabular-nums">{value}</p><p className="mt-0.5 text-[10px] uppercase tracking-wide text-amber-100/60">{label}</p></div>;
}

function RescuePanel({ job, rescue, busy, controls }: { job: Job; rescue: RescueStatus; busy: string | null; controls: DiscDockControls }) {
  const phase = rescue.phase ?? "starting";
  const retrying = RETRY_PHASES.includes(phase);
  const reading = job.state === "ripping" && (retrying || ["starting", "sweep"].includes(phase));
  const unreadable = rescue.movie_unreadable_bytes ?? rescue.unreadable_bytes ?? 0;
  const retry = phase === "sweep" ? rescue.deferred_bytes ?? 0 : rescue.movie_pending_bytes ?? rescue.pending_bytes ?? 0;
  const budget = rescue.extra_budget_seconds ?? 0;
  const used = Math.min(budget, rescue.extra_elapsed_seconds ?? 0);
  const movieOnly = rescue.scope === "movie";
  const phaseText = phase === "sweep" && movieOnly ? "Pass 1 of 2 · copying the movie and skipping past unreadable spots" : RESCUE_PHASES[phase] ?? "Rescuing readable sectors";
  const finish = () => {
    // Skipping the retries only gives up on spots that are already skipped, so it needs no confirmation.
    if (retrying) {
      void controls.finishRescue(job.id);
      return;
    }
    if (window.confirm(`Stop reading and build the movie from what has been rescued so far? The first pass has not reached the end of the ${movieOnly ? "movie" : "disc"} yet, so everything after the current position will be missing.`)) void controls.finishRescue(job.id);
  };
  return (
    <div className="mb-5 rounded-xl border border-amber-400/25 bg-amber-400/[0.06] p-4 text-amber-50">
      <div className="flex items-start gap-3">
        <LifeBuoy className="mt-0.5 size-5 shrink-0 text-amber-200" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">Damaged-disc rescue</p>
          <p className="mt-1 text-xs leading-5 text-amber-100/80">{phaseText}{phase === "sweep" && rescue.in_damaged_zone ? " · skipping past damage" : ""}{retrying && rescue.retry_round ? ` · round ${rescue.retry_round}` : ""}</p>
          {movieOnly && (rescue.not_needed_bytes ?? 0) > 0 && <p className="mt-0.5 text-[11px] leading-4 text-amber-100/60">Extras and menus ({formatSize(rescue.not_needed_bytes)}) are not read.</p>}
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-2 text-center sm:grid-cols-4">
        <RescueStat label="rescued" value={formatSize(rescue.rescued_bytes)} />
        <RescueStat label="unreadable" value={formatSize(unreadable)} />
        <RescueStat label={phase === "sweep" ? "set aside" : "left to retry"} value={formatSize(retry)} />
        <RescueStat label="read errors" value={String(rescue.read_errors ?? 0)} />
      </div>
      {retrying && budget > 0 && (
        <div className="mt-3">
          <div className="flex justify-between gap-3 text-[11px] text-amber-100/75"><span>Retry time · ends early once little more comes back</span><span className="shrink-0 tabular-nums">{Math.round(used / 60)} of {Math.round(budget / 60)} min</span></div>
          <Progress value={(used / budget) * 100} className="mt-1.5 h-1.5 bg-white/8 [&>div]:bg-amber-300" />
        </div>
      )}
      {reading && <Button disabled={busy !== null} variant="outline" size="sm" className={`mt-4 h-auto min-h-9 w-full whitespace-normal px-3 py-2 text-center leading-4 ${retrying ? "border-amber-300/60 bg-amber-300 font-semibold text-black hover:bg-amber-200" : "border-amber-300/25 bg-amber-100/5 text-amber-50 hover:bg-amber-100/10"}`} onClick={finish}>{busy === "finish-rescue" ? <Loader2 className="size-3.5 shrink-0 animate-spin" /> : retrying ? <SkipForward className="size-3.5 shrink-0" /> : <Check className="size-3.5 shrink-0" />} {retrying ? "Skip retrying · finish the movie now" : "Finish with what's rescued"}</Button>}
    </div>
  );
}

function WorkingTitleEditor({ job, busy, controls, onSaved }: { job: Job; busy: string | null; controls: DiscDockControls; onSaved: () => void }) {
  const [title, setTitle] = useState(job.title);
  const [year, setYear] = useState(job.year.slice(0, 4));
  const [matches, setMatches] = useState<MetadataCandidate[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedMatch, setSelectedMatch] = useState<MetadataCandidate | null>(() => candidateFromJob(job));
  const search = async () => {
    setHasSearched(true);
    const results = await controls.searchMetadata(title.trim(), year);
    if (results) setMatches(results);
  };
  const save = async () => {
    const metadata: MetadataCandidate = {
      ...(selectedMatch ?? {
        provider: "manual",
        provider_id: "",
        title: title.trim(),
        year,
        media_kind: job.media_kind,
        poster_url: "",
        plot: "",
        runtime_minutes: 0,
      }),
      title: title.trim(),
      year,
      user_selected: true,
    };
    const updated = await controls.updateMetadata(job.id, {
      title: title.trim(),
      year,
      media_kind: metadata.media_kind,
      metadata,
    });
    if (updated) onSaved();
  };
  const chosenPoster = safePoster(selectedMatch?.poster_url);
  return (
    <div className="mt-6 border-t border-white/8 pt-5">
      <div className="mb-4 flex items-center justify-between gap-3"><div><p className="text-sm font-medium">Search while DiscDock keeps working</p><p className="mt-1 text-xs text-muted-foreground">Choosing a result updates the cover and the final folder name.</p></div>{chosenPoster && <PosterArt compact url={chosenPoster} title={title || "this movie"} />}</div>
      <div className="space-y-2">
        <div className="relative"><PencilLine className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" /><Input aria-label="Running job title" value={title} onChange={(event) => { setTitle(event.target.value); setSelectedMatch(null); setHasSearched(false); }} placeholder="Movie title" className="border-white/10 bg-white/[0.025] pl-9" /></div>
        <div className="flex gap-2">
          <Input aria-label="Running job release year" value={year} maxLength={4} inputMode="numeric" onChange={(event) => { setYear(event.target.value.replace(/\D/g, "")); setSelectedMatch(null); setHasSearched(false); }} placeholder="Year" className="w-24 shrink-0 border-white/10 bg-white/[0.025]" />
          <Button disabled={busy !== null || title.trim().length < 2} variant="outline" size="sm" className="h-9 flex-1 gap-2 border-white/10 px-3" onClick={() => void search()}>{busy === "metadata-search" ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />} Search OMDb</Button>
        </div>
      </div>
      {matches.length > 0 && <div className="mt-3 max-h-56 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">{matches.map((match) => <button key={match.provider_id} type="button" className="flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-white/[0.045]" onClick={() => { setTitle(match.title); setYear(match.year.slice(0, 4)); setSelectedMatch(match); setMatches([]); }}><PosterArt compact url={safePoster(match.poster_url)} title={match.title} /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{match.title}</span><span className="mt-1 block text-xs text-muted-foreground">{match.year || "Year unknown"}</span></span></button>)}</div>}
      {hasSearched && matches.length === 0 && !selectedMatch && busy !== "metadata-search" && <p className="mt-3 rounded-lg border border-amber-400/15 bg-amber-400/7 px-3 py-2 text-xs text-amber-100">No OMDb match found. You can still save the title and year you entered.</p>}
      {selectedMatch?.provider === "omdb" && <p className="mt-3 text-xs text-emerald-300">Selected from OMDb · {selectedMatch.provider_id}</p>}
      <div className="mt-4 flex justify-end gap-2"><Button disabled={busy !== null} variant="ghost" size="sm" onClick={onSaved}>Cancel</Button><Button disabled={busy !== null || !title.trim()} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => void save()}>{busy === "update-metadata" ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />} Save title</Button></div>
    </div>
  );
}

const DISC_KIND_LABELS: Record<string, string> = {
  game: "Game or program disc",
  software: "Software disc",
  media: "Music or video files",
  pictures: "Pictures",
  documents: "Documents",
  files: "Files",
};

/** A data disc waiting to be recognised: it shows what is on the disc, then backs it up under a name. */
function DiscBackupCard({ job, busy, controls }: { job: Job; busy: string | null; controls: DiscDockControls }) {
  const contents = discContentsFromJob(job);
  const [title, setTitle] = useState(() => contents?.suggested_title || job.title || job.disc_label || "");
  const [year, setYear] = useState(job.year.slice(0, 4));
  const [filter, setFilter] = useState("");
  const [showAll, setShowAll] = useState(false);
  const entries = contents?.entries ?? [];
  const needle = filter.trim().toLowerCase();
  const matching = needle ? entries.filter((entry) => entry.path.toLowerCase().includes(needle)) : entries;
  const shown = showAll ? matching.slice(0, 2000) : matching.slice(0, 12);
  const folders = contents?.top_level ?? [];
  const canStart = title.trim().length > 0 && busy === null;
  return (
    <Card className="job-reveal border-primary/20 bg-card shadow-none">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-[0.16em] text-primary">Back up this disc</p>
            <CardTitle className="mt-1 truncate text-lg">{job.disc_label || "Data disc"}</CardTitle>
          </div>
          <Badge variant="outline" className="shrink-0 border-primary/25 text-primary">{DISC_KIND_LABELS[contents?.kind ?? "files"] ?? "Files"}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm leading-6 text-muted-foreground">
          {contents?.unreadable
            ? "Windows could not read this disc's file system, so its files cannot be listed. DiscDock can still copy the whole disc into an image."
            : `${contents?.summary ?? "Holds files."} ${contents?.file_count ?? 0} files, ${formatBytes(contents?.total_bytes ?? 0)}. The backup is an image of the whole disc: open it with a double-click and it appears as a drive.`}
          {contents?.note ? ` ${contents.note}` : ""}
        </p>
        {folders.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {folders.slice(0, 8).map((folder) => (
              <span key={folder.name} className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-1 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{folder.name}</span> · {folder.file_count} {folder.file_count === 1 ? "file" : "files"} · {formatBytes(folder.total_bytes)}
              </span>
            ))}
          </div>
        )}
        {entries.length > 0 && (
          <div className="rounded-xl border border-white/8 bg-black/10">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/8 px-3 py-2">
              <div className="relative min-w-[12rem] flex-1">
                <Search className="pointer-events-none absolute left-3 top-2.5 size-3.5 text-muted-foreground" />
                <Input aria-label="Search the files on this disc" value={filter} onChange={(event) => { setFilter(event.target.value); setShowAll(true); }} placeholder="Search the files on this disc" className="h-9 border-white/10 bg-white/[0.025] pl-8 text-xs" />
              </div>
              <span className="text-xs text-muted-foreground">{matching.length} of {entries.length} {entries.length === 1 ? "file" : "files"}</span>
            </div>
            <div className="max-h-56 overflow-y-auto divide-y divide-white/5">
              {shown.map((entry) => (
                <div key={entry.path} className="flex items-center justify-between gap-4 px-3 py-1.5 text-xs">
                  <span className="truncate font-mono text-muted-foreground">{entry.path}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">{formatBytes(entry.size)}</span>
                </div>
              ))}
              {shown.length === 0 && <p className="px-3 py-3 text-xs text-muted-foreground">No file on the disc matches that.</p>}
            </div>
            {!showAll && matching.length > shown.length && (
              <button type="button" className="w-full border-t border-white/8 px-3 py-2 text-xs font-medium text-primary hover:bg-white/[0.03]" onClick={() => setShowAll(true)}>Show all {matching.length} files</button>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[14rem] flex-1">
            <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor={`backup-name-${job.id}`}>What is this disc?</label>
            <Input id={`backup-name-${job.id}`} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Name for the backup" className="border-white/10 bg-white/[0.025]" />
          </div>
          <div className="w-28">
            <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor={`backup-year-${job.id}`}>Year</label>
            <Input id={`backup-year-${job.id}`} value={year} onChange={(event) => setYear(event.target.value.replace(/\D/g, "").slice(0, 4))} placeholder="Optional" className="border-white/10 bg-white/[0.025]" />
          </div>
          <Button disabled={!canStart} className="gap-2" onClick={() => void controls.backUpDisc(job.id, { title: title.trim(), year, media_kind: "other" })}>
            {busy === "back-up-disc" ? <Loader2 className="size-4 animate-spin" /> : <HardDrive className="size-4" />} Back up this disc
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ManualSelectionCard({ job, busy, controls }: { job: Job; busy: string | null; controls: DiscDockControls }) {
  const [selected, setSelected] = useState<number[]>(() => (job.tracks ?? []).filter((track) => track.selected).map((track) => track.source_id));
  const [title, setTitle] = useState(job.title);
  const [year, setYear] = useState(job.year.slice(0, 4));
  const [mediaKind, setMediaKind] = useState<MediaKind>(job.media_kind);
  const [matches, setMatches] = useState<MetadataCandidate[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedMatch, setSelectedMatch] = useState<MetadataCandidate | null>(() => candidateFromJob(job));
  const toggle = (id: number, checked: boolean) => setSelected((current) => checked ? [...current, id] : current.filter((value) => value !== id));
  const search = async () => {
    setHasSearched(true);
    const results = await controls.searchMetadata(title.trim(), year);
    if (results) setMatches(results);
  };
  const manualMetadata: MetadataCandidate = {
    ...(selectedMatch ?? {
      provider: "manual",
      provider_id: "",
      title: title.trim(),
      year,
      media_kind: mediaKind,
      poster_url: "",
      plot: "",
      runtime_minutes: 0,
    }),
    title: title.trim(),
    year,
    media_kind: mediaKind,
    user_selected: true,
  };
  const chosenPoster = safePoster(selectedMatch?.poster_url);
  // A disc completed before, getting more of its titles in the same library folder.
  const joining = addToExistingFromJob(job);
  const joiningFolder = joining ? joining.outputPath.split(/[\\/]/).filter(Boolean).pop() ?? joining.outputPath : "";
  return (
    <Card className="job-reveal border-primary/20 bg-card shadow-none">
      <CardHeader className="pb-3"><div className="flex items-start justify-between gap-4"><div className="min-w-0"><p className="text-xs font-medium uppercase tracking-[0.16em] text-primary">{joining ? "Add titles to a completed disc" : "Identify this disc"}</p><CardTitle className="mt-2 truncate text-xl">{title || "Untitled movie"}{year ? ` (${year})` : ""}</CardTitle></div><StateBadge state={job.state} /></div></CardHeader>
      <CardContent>
        {joining ? (
          <div className="space-y-3">
            <p className="text-sm leading-6 text-muted-foreground">The titles you select join the folder <span className="font-medium text-foreground">{joiningFolder}</span>. Titles ripped last time are unticked, new files are numbered after the ones already there, and nothing in the folder is replaced.</p>
            <div role="group" aria-label="What the new titles are" className="inline-flex flex-wrap rounded-lg border border-white/10 p-0.5 text-xs">
              {([["series", "Episodes of a series"], ["movie", "Extras of a movie"]] as const).map(([kind, label]) => <button key={kind} type="button" aria-pressed={mediaKind === kind} className={`rounded-md px-3 py-1.5 font-medium transition-colors ${mediaKind === kind ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`} onClick={() => setMediaKind(kind)}>{label}</button>)}
            </div>
          </div>
        ) : (
          <p className="text-sm leading-6 text-muted-foreground">Search OMDb for the correct movie, or type the title and year yourself. Then confirm which disc title to rip.</p>
        )}
        {/* The card can sit in a narrow column at any window size, so the fields stack instead of sharing one row. */}
        <div className="mt-4 flex flex-wrap items-start gap-5">
          <div className="min-w-[16rem] flex-1">
            <div className="space-y-2">
              <div className="relative">
                <PencilLine className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
                <Input aria-label="Media title" value={title} onChange={(event) => { setTitle(event.target.value); setSelectedMatch(null); setHasSearched(false); }} placeholder="Movie title" className="border-white/10 bg-white/[0.025] pl-9" />
              </div>
              <div className="flex gap-2">
                <Input aria-label="Release year" value={year} maxLength={4} inputMode="numeric" onChange={(event) => { setYear(event.target.value.replace(/\D/g, "")); setSelectedMatch(null); setHasSearched(false); }} placeholder="Year" className="w-24 shrink-0 border-white/10 bg-white/[0.025]" />
                <Button disabled={busy !== null || title.trim().length < 2} variant="outline" size="sm" className="h-9 flex-1 gap-2 border-white/10" onClick={() => void search()}>{busy === "metadata-search" ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />} Search OMDb</Button>
              </div>
            </div>
            {matches.length > 0 && <div className="mt-3 max-h-64 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">{matches.map((match) => <button key={match.provider_id} type="button" className="flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-white/[0.045]" onClick={() => { setTitle(match.title); setYear(match.year.slice(0, 4)); setMediaKind(match.media_kind); setSelectedMatch(match); setMatches([]); }}><PosterArt compact url={safePoster(match.poster_url)} title={match.title} /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{match.title}</span><span className="mt-1 block truncate text-xs text-muted-foreground">{match.media_kind.replace("_", " ")} · OMDb {match.provider_id}</span></span><span className="shrink-0 text-xs text-muted-foreground">{match.year}</span></button>)}</div>}
            {hasSearched && matches.length === 0 && !selectedMatch && busy !== "metadata-search" && <div className="mt-3 rounded-xl border border-amber-400/15 bg-amber-400/7 px-4 py-3 text-xs leading-5 text-amber-100">No OMDb result matched. You can change the search, or keep your manually entered title and year.</div>}
            {selectedMatch?.provider === "omdb" && selectedMatch.provider_id && <p className="mt-3 text-xs text-emerald-300">Matched with OMDb · {selectedMatch.provider_id}</p>}
          </div>
          <div className="flex w-[6.7rem] shrink-0 flex-col items-center">
            <PosterArt url={chosenPoster} title={title || "this movie"} />
            <p className="mt-2 w-full text-center text-[10px] uppercase tracking-[0.12em] text-muted-foreground">Cover art</p>
          </div>
        </div>
        <div className="mt-4 max-h-48 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">
          {(job.tracks ?? []).map((track) => (
            <label key={track.source_id} className="flex cursor-pointer items-center gap-3 px-3 py-3 hover:bg-white/[0.025]">
              <Checkbox checked={selected.includes(track.source_id)} onCheckedChange={(checked) => toggle(track.source_id, checked === true)} />
              <span className="min-w-0 flex-1 truncate text-sm">{track.name || `Title ${track.source_id}`}</span>
              {joining?.rippedTitles.includes(track.source_id) && <Badge variant="outline" className="shrink-0 border-emerald-400/25 text-emerald-300">In the library</Badge>}
              <span className="shrink-0 text-xs text-muted-foreground">{Math.round(track.duration_seconds / 60)} min</span>
            </label>
          ))}
          {!job.tracks?.length && <p className="px-4 py-6 text-center text-sm text-muted-foreground">No selectable video titles were found.</p>}
        </div>
        <div className="mt-5 flex items-center justify-between gap-3"><p className="text-xs text-muted-foreground">{selected.length} selected</p><div className="flex gap-2"><Button disabled={busy !== null} variant="ghost" size="sm" className="text-muted-foreground hover:text-red-300" onClick={() => void controls.cancel(job.id)}>Cancel</Button><Button disabled={busy !== null || selected.length === 0 || !title.trim()} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => void controls.continueJob(job.id, selected, { title: title.trim(), year, media_kind: mediaKind, metadata: manualMetadata })}>{busy === "continue" ? <Loader2 className="size-4 animate-spin" /> : <ListChecks className="size-4" />} {joining ? "Add to the folder" : "Rip selected"}</Button></div></div>
      </CardContent>
    </Card>
  );
}

// CDs whose tracks wait in staging for their album. They do not hold a drive, so they get cards of their own.
function WaitingCds({ jobs, busy, controls }: { jobs: Job[]; busy: string | null; controls: DiscDockControls }) {
  const waiting = jobs.filter((job) => job.state === "awaiting_album");
  if (waiting.length === 0) return null;
  return <div className="grid gap-6 lg:grid-cols-2">{waiting.map((job) => <WaitingCdCard key={job.id} job={job} busy={busy} controls={controls} />)}</div>;
}

function WaitingCdCard({ job, busy, controls }: { job: Job; busy: string | null; controls: DiscDockControls }) {
  const [finding, setFinding] = useState(false);
  const { album } = albumFromJob(job);
  const finishWithout = () => {
    const confirmed = window.confirm('Finish this CD without its album? The tracks go into your library with names like "01 - Unknown track".');
    if (confirmed) void controls.finishCd(job.id, true);
  };
  return (
    <Card className="border-amber-400/15 bg-card shadow-none">
      <CardHeader className="pb-3">
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
          <div className="min-w-0"><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Waiting in staging</p><CardTitle className="mt-2 break-words text-xl leading-snug">{titleFor(job)}</CardTitle><p className="mt-1 text-xs text-muted-foreground">{job.drive_letter} · ripped {formatDate(job.updated_at)}</p></div>
          <StateBadge state={job.state} />
        </div>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm leading-6 text-muted-foreground">{job.status_detail}</p>
        <AlbumStatus job={job} busy={busy} controls={controls} />
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy !== null || !album} title={album ? "Name and tag the tracks, then move them into the library" : "Find or enter the album first"} className="gap-2 bg-primary text-primary-foreground" onClick={() => void controls.finishCd(job.id, false)}>{busy === "finish-cd" ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Finish</Button>
          <Button disabled={busy !== null} variant="outline" className="gap-2 border-white/10 bg-transparent" onClick={() => setFinding((current) => !current)}><Search className="size-3.5" /> {finding ? "Close" : "Find the album"}</Button>
          <Button disabled={busy !== null} variant="ghost" className="text-muted-foreground" onClick={finishWithout}>Finish without album</Button>
        </div>
        {finding && <AlbumFinder job={job} busy={busy} controls={controls} onChosen={() => setFinding(false)} />}
      </CardContent>
    </Card>
  );
}

// The album of an audio CD from MusicBrainz, and the releases to choose from, while the CD keeps ripping.
function AlbumStatus({ job, busy, controls }: { job: Job; busy: string | null; controls: DiscDockControls }) {
  const { album, candidates, status, message, lookedUp, keepInStaging } = albumFromJob(job);
  const [chosen, setChosen] = useState("");
  if (!lookedUp && !album) return null;
  // Until the album is settled the CD can wait in staging for it. An album entered by hand is settled.
  const offerStaging = CD_BEFORE_TAGGING.has(job.state) && album?.source !== "manual" && (!album || ["busy", "error"].includes(status));
  const selected = chosen || album?.id || candidates[0]?.id || "";
  const selectedRelease = candidates.find((release) => release.id === selected);
  const warning = ["busy", "error", "not_found", "untagged"].includes(status) && Boolean(message);
  return (
    <div className="mb-5 rounded-xl border border-white/8 bg-white/[0.02] p-4">
      <div className="flex items-start gap-3">
        <Music className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">{album?.source === "manual" ? "Album entered by hand" : "Album from MusicBrainz"}</p>
          {album && <><p className="mt-1 break-words text-sm font-semibold">{albumName(album)}</p><p className="mt-1 break-words text-xs text-muted-foreground">{describeRelease(album, false)}</p></>}
          {album?.picked_first_of ? <p className="mt-1 text-xs text-amber-100">No release was chosen, so the first of {album.picked_first_of} is used.</p> : null}
          {!album && candidates.length === 0 && !warning && <p className="mt-1 flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-3.5 animate-spin" /> Looking up this CD</p>}
          {!album && candidates.length > 1 && <p className="mt-1 text-sm leading-6">{message}</p>}
        </div>
      </div>
      {candidates.length > 1 && (
        <>
          <RadioGroup value={selected} onValueChange={setChosen} aria-label="Releases of this CD" className="mt-3 max-h-56 gap-0 divide-y divide-white/7 overflow-y-auto rounded-lg border border-white/8">
            {candidates.map((release) => (
              <label key={release.id} className="flex cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-white/[0.025]">
                <RadioGroupItem value={release.id} />
                <ReleaseCover release={release} />
                <span className="min-w-0 flex-1"><span className="block break-words text-sm">{albumName(release)}</span><span className="mt-0.5 block break-words text-xs text-muted-foreground">{describeRelease(release)}</span></span>
              </label>
            ))}
          </RadioGroup>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            {selected ? <a href={`https://musicbrainz.org/release/${selected}`} target="_blank" rel="noreferrer" className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">Compare on MusicBrainz</a> : <span />}
            <Button disabled={busy !== null || !selectedRelease || selected === album?.id} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => { if (selectedRelease) void controls.chooseAlbum(job.id, selectedRelease); }}>{busy === "choose-album" ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} {selected === album?.id ? "In use" : "Use this release"}</Button>
          </div>
        </>
      )}
      {offerStaging && (
        <label className="mt-3 flex cursor-pointer items-start gap-3 border-t border-white/7 pt-3">
          <Checkbox className="mt-0.5" checked={keepInStaging} disabled={busy !== null} onCheckedChange={(checked) => void controls.keepCdInStaging(job.id, checked === true)} />
          <span className="min-w-0 text-xs leading-5"><span className="block text-sm font-medium">Keep in staging until the album is found</span><span className="text-muted-foreground">When the rip is done, the tracks wait here instead of going into your library without names, and the next CD can go in. A CD also waits when MusicBrainz is busy at the end. An album you enter yourself always finishes the CD.</span></span>
        </label>
      )}
      {album && status !== "tagged" && <AlbumCover job={job} album={album} busy={busy} controls={controls} />}
      {warning && <p role="status" className="mt-3 rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{message}</p>}
    </div>
  );
}

// The cover the tracks get: a photo of the front of the case, or else MusicBrainz's cover. When neither
// exists, for example for a release found by barcode that has no cover art, a photo can be added here.
function AlbumCover({ job, album, busy, controls }: { job: Job; album: AlbumRelease; busy: string | null; controls: DiscDockControls }) {
  const photoUrl = albumPhotoUrl(job, "front");
  const photo = useImageStatus(photoUrl);
  const archive = useImageStatus(archiveCoverUrl(album));
  const covers = useCoverEditor(job, controls);
  if (photo === "loaded") {
    return (
      <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-white/7 pt-3">
        <div role="img" aria-label="Cover photo" className="size-12 shrink-0 rounded-md border border-white/10 bg-cover bg-center" style={{ backgroundImage: `url("${photoUrl}")` }} />
        <p className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">Your photo of the front becomes the cover.</p>
        <div className="flex flex-wrap gap-1">
          <Button variant="ghost" size="sm" disabled={busy !== null || covers.opening} className="gap-2 text-muted-foreground" onClick={() => void covers.editSaved()}>{covers.opening ? <Loader2 className="size-3.5 animate-spin" /> : <Crop className="size-3.5" />} Edit</Button>
          <Button variant="ghost" size="sm" disabled={busy !== null} className="text-muted-foreground" onClick={covers.remove}>Remove the photo</Button>
        </div>
        {covers.problem && <p role="alert" className="w-full text-xs text-amber-100">{covers.problem}</p>}
        {covers.editor}
      </div>
    );
  }
  if (photo === "loading" || archive === "loading" || archive === "loaded") return covers.editor;
  return (
    <div className="mt-3 space-y-2 border-t border-white/7 pt-3">
      <p className="text-xs leading-5 text-muted-foreground">{album.source === "manual" ? "This album has no cover yet." : "MusicBrainz has no cover for this release."} Photograph the front of the case or choose a picture. You can crop and straighten it before it becomes the cover.</p>
      <PhotoCapture label="Photograph the front" disabled={busy !== null} onPhoto={covers.editNew} />
      {covers.editor}
    </div>
  );
}

// Edits a new photo of the front before it becomes the cover, or the saved cover again from the photo it was
// made from, and saves the cover, that photo and how it was edited.
function useCoverEditor(job: Job, controls: DiscDockControls) {
  const [session, setSession] = useState<{ photo: Blob; edit: CoverEdit | null; keepPhoto: boolean } | null>(null);
  const [opening, setOpening] = useState(false);
  const [problem, setProblem] = useState("");
  const key = `${job.id}:${albumFromJob(job).discid}`;
  const editNew = (photo: Blob) => {
    setProblem("");
    setSession({ photo, edit: null, keepPhoto: true });
  };
  const editSaved = async () => {
    setProblem("");
    setOpening(true);
    const original = albumPhotoUrl(job, "front_original");
    const photo = await fetch(original || albumPhotoUrl(job, "front")).then((response) => (response.ok ? response.blob() : null)).catch(() => null);
    setOpening(false);
    if (photo) setSession({ photo, edit: original ? readCoverEdit(key) : null, keepPhoto: !original });
    else setProblem("The saved cover could not be opened. Photograph the front again.");
  };
  const save = async (cover: Blob, edit: CoverEdit, photo: Blob | null) => {
    if (photo && !(await controls.uploadAlbumPhoto(job.id, "front_original", photo))) return false;
    if (!(await controls.uploadAlbumPhoto(job.id, "front", cover))) return false;
    writeCoverEdit(key, edit);
    setSession(null);
    return true;
  };
  const remove = () => {
    clearCoverEdit(key);
    void controls.removeAlbumPhoto(job.id, "front");
  };
  const editor = session ? <CoverEditor photo={session.photo} initial={session.edit} keepPhoto={session.keepPhoto} onCancel={() => setSession(null)} onDone={save} /> : null;
  return { editor, editNew, editSaved, remove, opening, problem };
}

// The front cover the Cover Art Archive has for a release, next to its name. A disc shows while it loads,
// and stays when the release has no cover there.
function ReleaseCover({ release }: { release: AlbumRelease }) {
  const url = archiveCoverUrl(release);
  const status = useImageStatus(url);
  return (
    <span aria-hidden="true" className="grid size-11 shrink-0 place-items-center overflow-hidden rounded-md border border-white/10 bg-white/[0.04] bg-cover bg-center" style={status === "loaded" ? { backgroundImage: `url("${url}")` } : undefined}>
      {status !== "loaded" && <Disc3 className={`size-4 text-muted-foreground/50 ${status === "loading" ? "animate-pulse" : ""}`} />}
    </span>
  );
}

function AlbumSearch({ job, busy, controls, onChosen }: { job: Job; busy: string | null; controls: DiscDockControls; onChosen: () => void }) {
  const { album, trackCount } = albumFromJob(job);
  const [query, setQuery] = useState(album ? [album.artist, album.title].filter(Boolean).join(" - ") : "");
  const [results, setResults] = useState<AlbumRelease[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [problem, setProblem] = useState("");
  const search = async () => {
    setSearching(true);
    setProblem("");
    try {
      setResults(await controls.searchAlbums(query.trim(), trackCount));
      setSearched(true);
    } catch (caught) {
      setResults([]);
      setProblem(caught instanceof Error ? caught.message : "The MusicBrainz search failed. Search again.");
    } finally {
      setSearching(false);
    }
  };
  const choose = async (release: AlbumRelease) => {
    if (await controls.chooseAlbum(job.id, release)) onChosen();
  };
  return (
    <div className="mt-4">
      <p className="text-sm font-medium">Search MusicBrainz by name while the CD keeps ripping</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">Type the artist and the album, for example “Tom Petty - Into the Great Wide Open”. The album you choose names and tags the tracks when the rip is done.</p>
      <form className="mt-3 flex gap-2" onSubmit={(event) => { event.preventDefault(); if (query.trim().length >= 2) void search(); }}>
        <Input aria-label="Artist and album" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Artist - Album" className="border-white/10 bg-white/[0.025]" />
        <Button type="submit" disabled={searching || query.trim().length < 2} variant="outline" size="sm" className="h-9 shrink-0 gap-2 border-white/10 px-3">{searching ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />} Search</Button>
      </form>
      {problem && <p role="alert" className="mt-3 rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{problem}</p>}
      {results.length > 0 && <div className="mt-3 max-h-72 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">{results.map((release) => <button key={release.id} type="button" disabled={busy !== null} className="flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-white/[0.045] disabled:opacity-60" onClick={() => void choose(release)}><ReleaseCover release={release} /><span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{albumName(release)}</span><span className="mt-1 block break-words text-xs text-muted-foreground">{describeRelease(release)}</span></span></button>)}</div>}
      {searched && !problem && results.length === 0 && <p className="mt-3 text-xs text-muted-foreground">No album matched. Try other words, or only the album title.</p>}
    </div>
  );
}

type FinderMode = "name" | "barcode" | "ocr";

// Ways to find a CD's album while it rips: MusicBrainz by name or by barcode, or entered by hand with OCR.
function AlbumFinder({ job, busy, controls, onChosen }: { job: Job; busy: string | null; controls: DiscDockControls; onChosen: () => void }) {
  const { lookedUp } = albumFromJob(job);
  const [mode, setMode] = useState<FinderMode>("name");
  const modes: [FinderMode, string][] = [["name", "Search by name"], ["barcode", "Barcode"], ["ocr", "Enter with OCR"]];
  return (
    <div className="mt-6 border-t border-white/8 pt-5">
      <div role="group" aria-label="How to find the album" className="inline-flex flex-wrap rounded-lg border border-white/10 p-0.5 text-xs">
        {modes.map(([value, label]) => <button key={value} type="button" aria-pressed={mode === value} className={`rounded-md px-3 py-1.5 font-medium transition-colors ${mode === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`} onClick={() => setMode(value)}>{label}</button>)}
      </div>
      {mode === "name" && <AlbumSearch job={job} busy={busy} controls={controls} onChosen={onChosen} />}
      {mode === "barcode" && <BarcodeSearch job={job} busy={busy} controls={controls} onChosen={onChosen} cameraReady={lookedUp} />}
      {mode === "ocr" && (lookedUp ? <ManualAlbumForm job={job} busy={busy} controls={controls} onSaved={onChosen} /> : <p className="mt-4 text-xs leading-5 text-muted-foreground">Entering the album with OCR becomes available as soon as cyanrip has read the CD, so DiscDock knows how many tracks it has.</p>)}
    </div>
  );
}

function ReleaseResults({ results, busy, onChoose }: { results: AlbumRelease[]; busy: string | null; onChoose: (release: AlbumRelease) => void }) {
  if (results.length === 0) return null;
  return (
    <div className="mt-3 max-h-72 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">
      {results.map((release) => <button key={release.id} type="button" disabled={busy !== null} className="flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-white/[0.045] disabled:opacity-60" onClick={() => onChoose(release)}><ReleaseCover release={release} /><span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{albumName(release)}</span><span className="mt-1 block break-words text-xs text-muted-foreground">{describeRelease(release)}</span></span></button>)}
    </div>
  );
}

function BarcodeSearch({ job, busy, controls, onChosen, cameraReady }: { job: Job; busy: string | null; controls: DiscDockControls; onChosen: () => void; cameraReady: boolean }) {
  const { trackCount } = albumFromJob(job);
  const [barcode, setBarcode] = useState("");
  const [scanning, setScanning] = useState(false);
  const [results, setResults] = useState<AlbumRelease[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [problem, setProblem] = useState("");
  const digits = barcode.replace(/\D/g, "");
  const { searchAlbumsByBarcode, chooseAlbum } = controls;
  const search = useCallback(async (code: string) => {
    setSearching(true);
    setProblem("");
    try {
      setResults(await searchAlbumsByBarcode(code, trackCount));
      setSearched(true);
    } catch (caught) {
      setResults([]);
      setProblem(caught instanceof Error ? caught.message : "The MusicBrainz search failed. Search again.");
    } finally {
      setSearching(false);
    }
  }, [searchAlbumsByBarcode, trackCount]);
  const scanned = useCallback((code: string) => {
    setScanning(false);
    setBarcode(code);
    void search(code);
  }, [search]);
  const choose = async (release: AlbumRelease) => {
    if (await chooseAlbum(job.id, release)) onChosen();
  };
  return (
    <div className="mt-4">
      <p className="text-sm font-medium">Find the CD by the barcode on the back of the case</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">Type the digits under the barcode{cameraReady ? ", or scan it with the camera" : ""}. A barcode finds the exact release, including its country and pressing.</p>
      <form className="mt-3 flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); if (digits.length >= 8) void search(digits); }}>
        <Input aria-label="Barcode digits" value={barcode} inputMode="numeric" onChange={(event) => setBarcode(event.target.value)} placeholder="7 029971 950223" className="min-w-[12rem] flex-1 border-white/10 bg-white/[0.025] font-mono" />
        <Button type="submit" disabled={searching || digits.length < 8} variant="outline" size="sm" className="h-9 gap-2 border-white/10 px-3">{searching ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />} Search</Button>
        {cameraReady && <Button type="button" variant="outline" size="sm" className="h-9 gap-2 border-white/10 px-3" onClick={() => setScanning((current) => !current)}><ScanBarcode className="size-4" /> {scanning ? "Close the camera" : "Scan with the camera"}</Button>}
      </form>
      {scanning && <BarcodeScanner onBarcode={scanned} />}
      {problem && <p role="alert" className="mt-3 rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{problem}</p>}
      <ReleaseResults results={results} busy={busy} onChoose={(release) => void choose(release)} />
      {searched && !problem && results.length === 0 && <p className="mt-3 text-xs leading-5 text-muted-foreground">No release on MusicBrainz has barcode {digits}. Search by name, or enter the album with OCR.</p>}
    </div>
  );
}

// What is typed or read from the case stays in this browser until the album is saved, and the photos are saved
// with the job as soon as they are taken, so the form shows all of it again when it is opened later.
function ManualAlbumForm({ job, busy, controls, onSaved }: { job: Job; busy: string | null; controls: DiscDockControls; onSaved: () => void }) {
  const { album, trackCount, discid } = albumFromJob(job);
  const draftKey = `${job.id}:${discid}`;
  const [form, setForm] = useState<AlbumDraft>(() => {
    const draft = readAlbumDraft(draftKey);
    const saved = album?.source === "manual" ? (album.tracks ?? []).map((track) => track.title) : [];
    const names = draft?.tracks ?? saved;
    return {
      artist: draft?.artist ?? album?.artist ?? "",
      title: draft?.title ?? album?.title ?? "",
      year: draft?.year ?? (album?.date ?? "").slice(0, 4),
      tracks: Array.from({ length: trackCount }, (_, index) => names[index] ?? ""),
    };
  });
  const [edited, setEdited] = useState(false);
  useEffect(() => { if (edited) writeAlbumDraft(draftKey, form); }, [draftKey, edited, form]);
  const edit = (change: (current: AlbumDraft) => AlbumDraft) => { setEdited(true); setForm(change); };
  const [reading, setReading] = useState<number | null>(null);
  const [readProblem, setReadProblem] = useState("");
  const front = useLoadedImage(albumPhotoUrl(job, "front"));
  const back = useLoadedImage(albumPhotoUrl(job, "back"));
  const covers = useCoverEditor(job, controls);

  const readBack = async (photo: Blob) => {
    setReadProblem("");
    setReading(0);
    try {
      const found = tracksFromText(await readCaseText(photo, setReading), trackCount);
      if (found.some(Boolean)) edit((current) => ({ ...current, tracks: found.map((name, index) => name || current.tracks[index] || "") }));
      else setReadProblem("No track names could be read from the photo. Take it closer, straight on and without glare, or type the names.");
    } catch (caught) {
      setReadProblem(`The photo could not be read${caught instanceof Error && caught.message ? `: ${caught.message}` : "."}`);
    } finally {
      setReading(null);
    }
  };
  const photographBack = (photo: Blob) => {
    void controls.uploadAlbumPhoto(job.id, "back", photo);
    void readBack(photo);
  };
  const readSavedBack = async () => {
    const photo = await fetch(albumPhotoUrl(job, "back")).then((response) => (response.ok ? response.blob() : null)).catch(() => null);
    if (photo) await readBack(photo);
    else setReadProblem("The saved photo of the back could not be opened. Photograph the back again.");
  };
  const save = async () => {
    const saved = await controls.saveManualAlbum(job.id, { artist: form.artist.trim(), title: form.title.trim(), year: form.year, tracks: form.tracks.map((name) => name.trim()) });
    if (!saved) return;
    setEdited(false);
    clearAlbumDraft(draftKey);
    onSaved();
  };
  const ready = form.artist.trim().length > 0 && form.title.trim().length > 0 && busy === null && reading === null;

  return (
    <div className="mt-4 space-y-5">
      <div>
        <p className="text-sm font-medium">Enter the album, with the track names read from the case</p>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">For a CD MusicBrainz does not know. Type the artist and the album, then photograph the back of the case to read the track names, and correct them if needed. A photo of the front becomes the cover, after you crop and straighten it. Everything here is kept when you close it, and the CD keeps ripping meanwhile.</p>
      </div>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_5.5rem]">
        <Input aria-label="Artist" value={form.artist} onChange={(event) => { const artist = event.target.value; edit((current) => ({ ...current, artist })); }} placeholder="Artist (required)" className="border-white/10 bg-white/[0.025]" />
        <Input aria-label="Album" value={form.title} onChange={(event) => { const title = event.target.value; edit((current) => ({ ...current, title })); }} placeholder="Album (required)" className="border-white/10 bg-white/[0.025]" />
        <Input aria-label="Year" value={form.year} maxLength={4} inputMode="numeric" onChange={(event) => { const year = event.target.value.replace(/\D/g, ""); edit((current) => ({ ...current, year })); }} placeholder="Year" className="border-white/10 bg-white/[0.025]" />
      </div>
      <div className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Track names · {trackCount} tracks on the CD</p>
        <div className="flex flex-wrap items-start gap-3">
          {back && <a href={back} target="_blank" rel="noreferrer" title="Open the photo of the back" className="block size-20 shrink-0 rounded-lg border border-white/10 bg-cover bg-center" style={{ backgroundImage: `url("${back}")` }}><span className="sr-only">Photo of the back of the case</span></a>}
          <div className="min-w-0 flex-1 space-y-2">
            <PhotoCapture label={back ? "Photograph the back again" : "Photograph the back"} disabled={reading !== null || busy !== null} onPhoto={photographBack} />
            {back && <div className="flex flex-wrap gap-1"><Button variant="ghost" size="sm" disabled={reading !== null} className="text-muted-foreground" onClick={() => void readSavedBack()}>Read the names again</Button><Button variant="ghost" size="sm" disabled={busy !== null} className="text-muted-foreground" onClick={() => void controls.removeAlbumPhoto(job.id, "back")}>Remove the photo</Button></div>}
          </div>
        </div>
        {reading !== null && <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" /> Reading the track names… {Math.round(reading * 100)}%</p>}
        {readProblem && <p role="alert" className="rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{readProblem}</p>}
        <ol className="max-h-80 space-y-1.5 overflow-y-auto pr-1">
          {form.tracks.map((name, index) => (
            <li key={index} className="flex items-center gap-2">
              <span className="w-6 shrink-0 text-right font-mono text-xs text-muted-foreground">{index + 1}</span>
              <Input aria-label={`Name of track ${index + 1}`} value={name} onChange={(event) => { const typed = event.target.value; edit((current) => ({ ...current, tracks: current.tracks.map((value, position) => (position === index ? typed : value)) })); }}placeholder={`Track ${String(index + 1).padStart(2, "0")}`} className="h-8 border-white/10 bg-white/[0.025] text-sm" />
            </li>
          ))}
        </ol>
      </div>
      <div className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Cover</p>
        <div className="flex flex-wrap items-start gap-3">
          {front && <div role="img" aria-label="Cover photo" className="size-20 shrink-0 rounded-lg border border-white/10 bg-cover bg-center" style={{ backgroundImage: `url("${front}")` }} />}
          <div className="min-w-0 flex-1 space-y-2">
            <PhotoCapture label={front ? "Photograph the front again" : "Photograph the front"} disabled={busy !== null} onPhoto={covers.editNew} />
            {front && <div className="flex flex-wrap gap-1"><Button variant="ghost" size="sm" disabled={busy !== null || covers.opening} className="gap-2 text-muted-foreground" onClick={() => void covers.editSaved()}>{covers.opening ? <Loader2 className="size-3.5 animate-spin" /> : <Crop className="size-3.5" />} Edit the cover</Button><Button variant="ghost" size="sm" disabled={busy !== null} className="text-muted-foreground" onClick={covers.remove}>Remove the cover photo</Button></div>}
            {covers.problem && <p role="alert" className="text-xs text-amber-100">{covers.problem}</p>}
          </div>
          {covers.editor}
        </div>
      </div>
      <div className="flex justify-end">
        <Button disabled={!ready} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => void save()}>{busy === "manual-album" || busy === "album-photo" ?<Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Use this album</Button>
      </div>
    </div>
  );
}

function recentJobDetail(job: Job): string {
  const { album } = albumFromJob(job);
  if (job.disc_type !== "audio_cd" || !album) return job.status_detail;
  const picked = album.picked_first_of ? `first of ${album.picked_first_of} releases, none was chosen` : "";
  return [describeRelease(album), picked].filter(Boolean).join(" · ") || job.status_detail;
}

// An audio CD MusicBrainz knows in several releases waits here, as a DVD waits for its titles.
function ReleaseChoiceCard({ job, busy, controls }: { job: Job; busy: string | null; controls: DiscDockControls }) {
  const releases = musicReleasesFromJob(job);
  const [chosen, setChosen] = useState(releases[0]?.id ?? "");
  return (
    <Card className="job-reveal border-primary/20 bg-card shadow-none">
      <CardHeader className="pb-3"><div className="flex items-start justify-between gap-4"><div className="min-w-0"><p className="text-xs font-medium uppercase tracking-[0.16em] text-primary">Choose the release</p><CardTitle className="mt-2 break-words text-xl leading-snug">Which release is this CD?</CardTitle></div><StateBadge state={job.state} /></div></CardHeader>
      <CardContent>
        <p className="text-sm leading-6 text-muted-foreground">MusicBrainz lists {releases.length} releases of this CD. They usually differ only in country, label or pressing. The one you choose sets the album and track names.</p>
        <RadioGroup value={chosen} onValueChange={setChosen} aria-label="MusicBrainz releases" className="mt-4 max-h-72 gap-0 divide-y divide-white/7 overflow-y-auto rounded-xl border border-white/8">
          {releases.map((release, index) => (
            <label key={release.id} className="flex cursor-pointer items-start gap-3 px-3 py-3 hover:bg-white/[0.025]">
              <RadioGroupItem value={release.id} className="mt-0.5" />
              <span className="min-w-0 flex-1">
                <span className="block break-words text-sm font-medium">{release.title}</span>
                <a href={`https://musicbrainz.org/release/${release.id}`} target="_blank" rel="noreferrer" className="mt-1 inline-block text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">Release {index + 1} on MusicBrainz</a>
              </span>
            </label>
          ))}
          <label className="flex cursor-pointer items-start gap-3 px-3 py-3 hover:bg-white/[0.025]">
            <RadioGroupItem value="none" className="mt-0.5" />
            <span className="min-w-0 flex-1"><span className="block text-sm font-medium">Rip without track names</span><span className="mt-1 block text-xs text-muted-foreground">No album or track names from MusicBrainz.</span></span>
          </label>
        </RadioGroup>
        <div className="mt-5 flex items-center justify-end gap-2"><Button disabled={busy !== null} variant="ghost" size="sm" className="text-muted-foreground hover:text-red-300" onClick={() => void controls.cancel(job.id)}>Cancel</Button><Button disabled={busy !== null || !chosen} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => void controls.chooseRelease(job.id, chosen)}>{busy === "choose-release" ? <Loader2 className="size-4 animate-spin" /> : <Disc3 className="size-4" />} {chosen === "none" ? "Rip without names" : "Rip this release"}</Button></div>
      </CardContent>
    </Card>
  );
}

type JobAction = {
  key: string;
  label: string;
  detail: string;
  busyKey: string;
  icon: ReactNode;
  run: () => void;
};

/** What can still be done with a job, most useful first. The first one is the button; the rest wait in the menu. */
function jobActions(job: Job, controls: DiscDockControls): JobAction[] {
  const actions: JobAction[] = [];
  // Repair works through every method by itself, so a separate retry would do the same thing.
  const repairing = offerDamageRecovery(job) && job.error_code !== "ai_repair_not_possible";
  if (repairing) {
    actions.push({
      key: "repair",
      label: "Repair",
      detail: "Read what the drive still can and work through the repair methods, fastest first",
      busyKey: "recover-damaged",
      icon: <LifeBuoy className="size-3.5" />,
      run: () => confirmDamageRecovery(job, controls),
    });
  }
  if (offerFinishRescued(job)) {
    actions.push({
      key: "finish",
      label: "Finish with what's rescued",
      detail: "Stop reading the disc and finish the movie from what has been rescued so far",
      busyKey: "finish-rescue",
      icon: <Check className="size-3.5" />,
      run: () => confirmFinishRescued(job, controls),
    });
  }
  if (offerAiRepair(job)) {
    actions.push({
      key: "ai",
      label: "AI estimate",
      detail: "Find the damaged moments and price replacing them with AI frames — free, nothing is sent",
      busyKey: "prepare-ai-repair",
      icon: <Sparkles className="size-3.5" />,
      run: () => void controls.prepareAiRepair(job.id),
    });
  }
  if (job.recoverable && !repairing) {
    actions.push({
      key: "retry",
      label: retryLabel(job),
      detail: "Start this disc again",
      busyKey: "retry",
      icon: <RotateCw className="size-3.5" />,
      run: () => void controls.retry(job.id),
    });
  }
  return actions;
}

function JobActions({ job, controls, busy }: { job: Job; controls: DiscDockControls; busy: string | null }) {
  const actions = jobActions(job, controls);
  const [first, ...rest] = actions;
  if (!first) return null;
  return (
    <>
      <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-amber-400/20 text-amber-100" title={first.detail} onClick={first.run}>{busy === first.busyKey ? <Loader2 className="size-3.5 animate-spin" /> : first.icon} {first.label}</Button>
      {rest.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy !== null} variant="ghost" size="sm" className="gap-1 px-2 text-muted-foreground" aria-label={`More ways to finish ${titleFor(job)}`}><MoreHorizontal className="size-4" /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-64">
            {rest.map((action) => (
              <DropdownMenuItem key={action.key} onSelect={action.run} className="gap-2">
                {action.icon}
                <span className="min-w-0"><span className="block truncate">{action.label}</span><span className="block text-xs leading-4 text-muted-foreground whitespace-normal">{action.detail}</span></span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </>
  );
}

function RecentJobs({ jobs, busy, controls }: { jobs: Job[]; busy: string | null; controls: DiscDockControls }) {
  return (
    <Card className="border-white/8 bg-card shadow-none">
      <CardHeader className="pb-2"><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Recent jobs</p><CardTitle className="mt-1 text-lg">Your latest discs</CardTitle></CardHeader>
      <CardContent className="divide-y divide-white/7 px-0 pb-0">
        {jobs.length === 0 && <div className="px-6 py-10 text-center text-sm text-muted-foreground">No completed or failed jobs yet.</div>}
        {jobs.map((job) => (
          <div key={job.id} className="flex items-center gap-4 px-6 py-4 transition-colors hover:bg-white/[0.025]">
            <div className={`grid size-10 shrink-0 place-items-center rounded-xl ${job.state === "completed" ? "bg-emerald-400/10 text-emerald-300" : "bg-amber-400/10 text-amber-300"}`}>{job.state === "completed" ? <Check className="size-4" /> : <Sparkles className="size-4" />}</div>
            <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{titleFor(job)}</p><p className="mt-1 truncate text-xs text-muted-foreground">{job.disc_type.replace("_", " ")} · {recentJobDetail(job)}</p></div>
            <div className="hidden text-right sm:block"><StateBadge state={job.state} /><p className="mt-1 text-xs text-muted-foreground">{formatDate(job.updated_at)}</p></div>
            {job.error_code === "makemkv_license" && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-amber-400/20 text-amber-100" onClick={() => void controls.openMakeMKV()}><KeyRound className="size-3.5" /> Activate MakeMKV</Button>}
            <JobActions job={job} controls={controls} busy={busy} />
            {job.state === "completed" && Boolean(discReadWarning(job)) && offerDamageReview(job) && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10" title="Decode the whole movie to find every broken part, then choose loading screens or AI frames" onClick={() => { if (window.confirm("Look through this movie for broken parts? DiscDock decodes the whole movie to find where the picture breaks up, not only where the disc gave nothing at all. It takes a few minutes, changes nothing and costs nothing. Afterwards you choose what to do with what it finds.")) void controls.reviewDamage(job.id); }}>{busy === "review-damage" ? <Loader2 className="size-3.5 animate-spin" /> : <Wrench className="size-3.5" />} Replace broken parts</Button>}
            {damageDetailsFromJob(job).choicePending && (
              <>
                <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10" title="Keep the movie as it was read, frozen where the disc was damaged" onClick={() => void controls.keepDamagedMovie(job.id)}>{busy === "keep-damaged-movie" && <Loader2 className="size-3.5 animate-spin" />} Keep as it is</Button>
                <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-amber-400/20 text-amber-100" onClick={() => { if (window.confirm("Add loading screens to this movie? Where the disc was damaged for 2 seconds or more, it then shows a loading screen with the time the movie continues instead of a frozen picture. Only a few seconds around those moments are encoded again, and the movie as it was read is kept next to it.")) void controls.addLoadingScreens(job.id); }}>{busy === "loading-screens" ? <Loader2 className="size-3.5 animate-spin" /> : <Film className="size-3.5" />} Add loading screens</Button>
                {aiEstimateFromJob(job) && <Button disabled={busy !== null} size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={() => { const plan = aiEstimateFromJob(job); if (plan && window.confirm(`Replace the broken parts with AI-generated frames? This sends pictures from either side of each damaged moment to OpenAI and costs at most $${plan.estimated_max_cost_usd.toFixed(2)}. You review the estimate before anything is sent. The movie without AI frames is kept next to the repaired one.`)) void controls.prepareAiRepair(job.id); }}>{busy === "prepare-ai-repair" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />} Replace with AI · up to ${aiEstimateFromJob(job)!.estimated_max_cost_usd.toFixed(2)}</Button>}
              </>
            )}
            {isCompletedDuplicate(job) && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-primary/25 text-primary" title="Rip more titles of this disc into the folder it was completed in" onClick={() => void controls.addTitlesToCompleted(job.id)}>{busy === "add-titles" ? <Loader2 className="size-3.5 animate-spin" /> : <ListChecks className="size-3.5" />} Add titles to the existing folder</Button>}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function SystemHealth({ data, openSettings }: { data: Bootstrap | null; openSettings: () => void }) {
  const tools = data?.health.tools ?? {};
  const rows = [
    ["MakeMKV", tools.makemkv, true], ["FFmpeg verification", tools.ffprobe, true],
    ["HandBrake", tools.handbrake, false], ["Audio CDs", tools.cyanrip, false],
    ["OMDb metadata", Boolean(data?.settings.secrets?.omdb_api_key), false],
    ["OpenAI repair", Boolean(data?.settings.secrets?.openai_api_key), false],
  ] as const;
  return (
    <Card className="border-white/8 bg-card shadow-none">
      <CardHeader className="pb-4"><div className="flex items-center justify-between"><div><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">System</p><CardTitle className="mt-2 text-lg">{data?.health.ok ? "Core ready" : "Setup required"}</CardTitle></div><Settings2 className="size-5 text-muted-foreground" /></div></CardHeader>
      <CardContent className="space-y-4">
        {rows.map(([label, ok, required]) => <div key={label} className="flex items-center justify-between gap-4"><div className="flex items-center gap-2.5"><span className={`size-1.5 rounded-full ${ok ? "bg-emerald-400" : required ? "bg-red-400" : "bg-amber-400"}`} /><span className="text-sm text-muted-foreground">{label}</span></div><span className="text-sm font-medium">{ok ? "Ready" : required ? "Missing" : "Optional"}</span></div>)}
        <Button variant="outline" className="mt-2 w-full border-white/10 bg-white/[0.025]" onClick={openSettings}>Open settings</Button>
      </CardContent>
    </Card>
  );
}

function DashboardSkeleton() {
  return <div className="grid gap-6 xl:grid-cols-2"><Skeleton className="h-[330px] rounded-2xl bg-white/5" /><Skeleton className="h-[330px] rounded-2xl bg-white/5" /><Skeleton className="h-[310px] rounded-2xl bg-white/5" /><Skeleton className="h-[310px] rounded-2xl bg-white/5" /></div>;
}
