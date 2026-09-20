"use client";

import { useState } from "react";
import { AlertTriangle, Disc3, FileText, Film, FolderOpen, KeyRound, Loader2, RefreshCw, Sparkles, Wrench } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { aiRepairFromJob, api, apiUrl, damageDetailsFromJob, damageMomentsFromJob, offerDamageReview, type DamageMoment, type DamageTreatment, type Job } from "@/lib/discdock-api";
import type { DiscDockControls } from "@/hooks/use-discdock";
import { formatBytes, formatDate, StateBadge, titleFor } from "./status";

export function HistoryView({ jobs, busy, controls, libraryOnly = false }: {
  jobs: Job[];
  busy: string | null;
  controls: DiscDockControls;
  libraryOnly?: boolean;
}) {
  const [selected, setSelected] = useState<Job | null>(null);
  const showDetails = async (job: Job) => {
    setSelected(job);
    try {
      setSelected(await api<Job>(`/api/v1/jobs/${job.id}`));
    } catch {
      // The summary remains useful if a detail refresh fails momentarily.
    }
  };
  const visible = libraryOnly ? jobs.filter((job) => job.state === "completed") : jobs;
  const selectedMetadata = selected?.metadata ?? {};
  const metadataProvider = typeof selectedMetadata.provider === "string" ? selectedMetadata.provider : "";
  const metadataId = typeof selectedMetadata.provider_id === "string" ? selectedMetadata.provider_id : "";
  const metadataPlot = typeof selectedMetadata.plot === "string" ? selectedMetadata.plot : "";
  const metadataPoster = typeof selectedMetadata.poster_url === "string" && selectedMetadata.poster_url.startsWith("https://") ? selectedMetadata.poster_url : "";
  const aiRepair = selected ? aiRepairFromJob(selected) : null;
  const damage = selected ? damageMomentsFromJob(selected) : [];
  const damageDetails = selected ? damageDetailsFromJob(selected) : null;
  const screensPossible = selected?.state === "completed" && damage.some(needsLoadingScreen);
  const addLoadingScreens = (job: Job) => {
    const confirmed = window.confirm("Add loading screens to this movie? Where the disc was damaged for 2 seconds or more, the movie then shows a loading screen with the time it continues instead of a frozen picture, and a chapter mark lets you skip it. Only a few seconds around those moments are encoded again; the rest of the video, the audio and the subtitles are copied unchanged. The movie without loading screens is kept next to it.");
    if (confirmed) void controls.addLoadingScreens(job.id);
  };
  const aiEstimate = aiRepair && aiRepair.status !== "applied" && aiRepair.segments.length > 0 ? aiRepair : null;
  // The estimate can come back with nothing to offer: AI only bridges gaps of up to four seconds.
  const aiTooLong = aiRepair && aiRepair.status !== "applied" && aiRepair.segments.length === 0 && (aiRepair.skipped?.length ?? 0) > 0 ? aiRepair : null;
  const reviewDamage = (job: Job) => {
    const confirmed = window.confirm("Look through this movie for broken parts? DiscDock decodes the whole movie to find where the picture breaks up, not only where the disc gave nothing at all. It takes a few minutes, changes nothing and costs nothing. Afterwards you choose what to do with what it finds.");
    if (confirmed) void controls.reviewDamage(job.id).then((updated) => updated && setSelected(updated));
  };
  const repairWithAi = (job: Job) => {
    const ceiling = aiEstimate ? `$${aiEstimate.estimated_max_cost_usd.toFixed(2)}` : "the estimate";
    const confirmed = window.confirm(`Replace the broken parts with AI-generated frames? This sends pictures from either side of each damaged moment to OpenAI and costs at most ${ceiling}. You review the estimate before anything is sent. The movie without AI frames is kept next to the repaired one.`);
    if (confirmed) void controls.prepareAiRepair(job.id).then((updated) => updated && setSelected(updated));
  };
  return (
    <>
      <Card className="border-white/8 bg-card shadow-none">
        <CardHeader className="flex-row items-end justify-between gap-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">{libraryOnly ? "Completed media" : "Job records"}</p>
            <CardTitle className="mt-2 text-xl">{libraryOnly ? "Library" : "History"}</CardTitle>
          </div>
          <p className="text-sm text-muted-foreground">{visible.length} {visible.length === 1 ? "item" : "items"}</p>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {visible.length === 0 ? (
            <div className="flex min-h-[360px] flex-col items-center justify-center px-6 text-center">
              <div className="grid size-12 place-items-center rounded-2xl bg-primary/8 text-primary"><Disc3 className="size-5" /></div>
              <h2 className="mt-5 text-lg font-semibold">{libraryOnly ? "No completed media yet" : "No job history yet"}</h2>
              <p className="mt-2 max-w-sm text-sm leading-6 text-muted-foreground">Insert a disc and start a scan from the dashboard. Every stage, interruption, and completed output will be recorded here.</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader><TableRow className="border-white/8 hover:bg-transparent"><TableHead className="pl-6">Title</TableHead><TableHead>Status</TableHead><TableHead>Drive</TableHead><TableHead>Updated</TableHead><TableHead className="pr-6 text-right">Actions</TableHead></TableRow></TableHeader>
                <TableBody>
                  {visible.map((job) => (
                    <TableRow key={job.id} className="border-white/7 hover:bg-white/[0.025]">
                      <TableCell className="max-w-[360px] pl-6"><button className="block max-w-full text-left" onClick={() => void showDetails(job)}><span className="block truncate font-medium">{titleFor(job)}</span><span className="mt-1 block truncate text-xs text-muted-foreground">{job.disc_type.replace("_", " ")} · {job.status_detail}</span>{damageSummary(job) && <span className="mt-1 block truncate text-xs text-amber-200/85">{damageSummary(job)}</span>}</button></TableCell>
                      <TableCell><StateBadge state={job.state} /></TableCell>
                      <TableCell className="text-muted-foreground">{job.drive_letter}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{formatDate(job.updated_at)}</TableCell>
                      <TableCell className="pr-6 text-right">
                        <div className="flex justify-end gap-1">
                          {job.output_path && <Button title="Open output" variant="ghost" size="icon" onClick={() => void controls.openOutput(job.id)}><FolderOpen className="size-4" /></Button>}
                          {job.recoverable && <Button title="Retry job" disabled={busy !== null} variant="ghost" size="icon" onClick={() => void controls.retry(job.id)}><RefreshCw className="size-4" /></Button>}
                          <Button title="View details" variant="ghost" size="icon" onClick={() => void showDetails(job)}><FileText className="size-4" /></Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[88vh] overflow-y-auto border-white/10 bg-[#0c171a] sm:max-w-2xl">
          {selected && (
            <>
              <DialogHeader>
                <div className="mb-2"><StateBadge state={selected.state} /></div>
                <DialogTitle className="text-xl">{titleFor(selected)}</DialogTitle>
                <DialogDescription>{selected.disc_label || "No disc label"} · {selected.drive_letter} · {formatDate(selected.created_at)}</DialogDescription>
              </DialogHeader>
              {(metadataPoster || metadataPlot || metadataProvider) && <div className="flex gap-4 rounded-xl border border-white/8 bg-white/[0.02] p-4"><div aria-label={metadataPoster ? `Poster for ${titleFor(selected)}` : undefined} className="hidden h-28 w-20 shrink-0 rounded-lg bg-white/5 bg-cover bg-center sm:block" style={metadataPoster ? { backgroundImage: `url(${JSON.stringify(metadataPoster)})` } : undefined} /><div className="min-w-0"><p className="text-xs font-medium uppercase tracking-[0.14em] text-primary">{metadataProvider || "Disc metadata"}{metadataId ? ` · ${metadataId}` : ""}</p><p className="mt-2 text-sm leading-6 text-muted-foreground">{metadataPlot || "Title and release information matched for this disc."}</p></div></div>}
              {aiRepair?.status === "applied" && <div className="rounded-xl border border-primary/20 bg-primary/6 p-4"><div className="flex items-start gap-3"><Sparkles className="mt-0.5 size-5 shrink-0 text-primary" /><div><p className="text-sm font-semibold">AI repair used</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{aiRepair.frame_count} generated frames from {aiRepair.ai_keyframe_count} OpenAI images · OpenAI cost ${(aiRepair.actual_cost_usd ?? 0).toFixed(4)}.</p></div></div><p className="mt-3 rounded-lg border border-amber-400/15 bg-amber-400/7 px-3 py-2 text-xs leading-5 text-amber-100">These frames were generated, not recovered from the disc. Audio was left unchanged.</p><div className="mt-4 space-y-4">{aiRepair.segments.map((segment) => <div key={segment.index} className="rounded-lg border border-white/8 bg-black/10 p-3"><div className="flex flex-wrap items-center justify-between gap-2 text-xs"><span className="font-medium">Repair {segment.index} · {segment.frame_count} frames</span><span className="font-mono text-muted-foreground">{formatRepairTime(segment.start_seconds)} – {formatRepairTime(segment.end_seconds)}</span></div><video className="mt-3 aspect-video w-full rounded-lg bg-black" controls preload="metadata" src={apiUrl(`/api/v1/jobs/${selected.id}/ai-repair/preview/${segment.index}`)}>Your browser cannot play this AI repair preview.</video></div>)}</div></div>}
              {selected.stage === "damage_scan" && (
                <div className="rounded-xl border border-primary/20 bg-primary/6 p-4">
                  <div className="flex items-start gap-3">
                    <Loader2 className="mt-0.5 size-5 shrink-0 animate-spin text-primary" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold">Looking through the movie for broken parts</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">Every frame is decoded to find where the picture breaks up. The movie is not changed, and nothing is sent anywhere. The choice of what to do appears here when it is done.</p>
                    </div>
                  </div>
                </div>
              )}
              {offerDamageReview(selected) && selected.stage !== "damage_scan" && (
                <div className="rounded-xl border border-white/8 bg-white/[0.02] p-4">
                  <div className="flex items-start gap-3">
                    <Wrench className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold">Broken parts</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">{damage.length > 0 ? "The moments below come from the quick check after the rip, which only sees what the disc never gave at all." : "A movie can play through and still break up where the disc was scratched."} DiscDock can decode the whole movie to find every broken part and then offer loading screens or AI-generated frames for them. It takes a few minutes, changes nothing and costs nothing.</p>
                    </div>
                  </div>
                  <div className="mt-4 flex justify-end">
                    <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10" onClick={() => reviewDamage(selected)}>{busy === "review-damage" ? <Loader2 className="size-3.5 animate-spin" /> : <Wrench className="size-3.5" />} Replace broken parts</Button>
                  </div>
                </div>
              )}
              {damage.length > 0 && (
                <div className="rounded-xl border border-amber-400/15 bg-amber-400/[0.05] p-4">
                  <div className="flex items-start gap-3">
                    <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-300" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold">Where the disc was damaged</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">{damage.length} {damage.length === 1 ? "moment" : "moments"} could not be read from the disc. Times are positions in the movie.</p>
                    </div>
                  </div>
                  <div className="mt-3 divide-y divide-white/7 rounded-lg border border-white/8">
                    {damage.map((moment) => (
                      <div key={`${moment.start_seconds}-${moment.end_seconds}`} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2 text-xs">
                        <span className="font-mono">{formatClock(moment.start_seconds)} – {formatClock(moment.end_seconds, true)}</span>
                        <span className="text-muted-foreground">{formatLength(moment.duration_seconds)}</span>
                        <span className={`rounded-full px-2 py-0.5 ${TREATMENT_STYLES[moment.treatment ?? "skipped"]}`}>{TREATMENT_LABELS[moment.treatment ?? "skipped"]}</span>
                      </div>
                    ))}
                  </div>
                  {damageDetails?.keptCopy && (
                    <p className="mt-3 text-xs leading-5 text-muted-foreground">
                      The movie as it was read from the disc is kept in the same folder, named “… - without {aiRepair?.status === "applied" ? "AI frames" : "loading screens"}”.
                      {damageDetails.loadingScreenMethod === "splice" && " Only the seconds around the loading screens were encoded again; the rest is the original video."}
                      {damageDetails.loadingScreenMethod === "reencode" && " This movie's video format could not be cut at its keyframes, so the whole video was encoded again."}
                    </p>
                  )}
                  {(screensPossible || aiEstimate) && selected.state === "completed" && (
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                      <p className="max-w-md text-xs leading-5 text-muted-foreground">{damageDetails?.choicePending ? "Choose what happens at these moments: keep the movie as it was read, cover them with a loading screen, or let AI draw the missing frames." : "Frozen moments can show a loading screen that says when the movie continues, with a chapter mark to skip it."}</p>
                      <div className="flex flex-wrap gap-2">
                        {damageDetails?.choicePending && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-white/10" onClick={() => void controls.keepDamagedMovie(selected.id).then((job) => job && setSelected(job))}>{busy === "keep-damaged-movie" && <Loader2 className="size-3.5 animate-spin" />} Keep as it is</Button>}
                        {screensPossible && <Button disabled={busy !== null} variant="outline" size="sm" className="gap-2 border-amber-400/25 text-amber-100" onClick={() => addLoadingScreens(selected)}>{busy === "loading-screens" ? <Loader2 className="size-3.5 animate-spin" /> : <Film className="size-3.5" />} Add loading screens</Button>}
                        {aiEstimate && <Button disabled={busy !== null} size="sm" className="gap-2 bg-primary text-primary-foreground" title={`${aiEstimate.frame_count} frames from ${aiEstimate.ai_keyframe_count} OpenAI images`} onClick={() => repairWithAi(selected)}>{busy === "prepare-ai-repair" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />} Replace with AI · up to ${aiEstimate.estimated_max_cost_usd.toFixed(2)}</Button>}
                      </div>
                    </div>
                  )}
                  {aiTooLong && (
                    <p className="mt-3 flex items-start gap-2 text-xs leading-5 text-muted-foreground">
                      <Sparkles className="mt-0.5 size-3.5 shrink-0" />
                      <span>{aiTooLong.summary || "The damage is too long for AI-generated frames."} AI can only draw over a gap of up to four seconds.</span>
                    </p>
                  )}
                </div>
              )}
              <div className="grid gap-3 rounded-xl border border-white/8 bg-white/[0.02] p-4 text-sm sm:grid-cols-2">
                <Detail label="Stage" value={selected.stage} />
                <Detail label="Progress" value={`${Math.round(selected.progress)}%`} />
                <Detail label="Media type" value={selected.media_kind} />
                <Detail label="Disc type" value={selected.disc_type.replace("_", " ")} />
                <Detail label="Output" value={selected.output_path || "Not finalized"} wide />
                <Detail label="Staging" value={selected.staging_path || "—"} wide />
              </div>
              {selected.error_code === "makemkv_license" ? (
                <div className="flex gap-3 rounded-xl border border-amber-400/20 bg-amber-400/8 p-4 text-sm text-amber-50">
                  <KeyRound className="mt-1 size-4 shrink-0" />
                  <div className="min-w-0">
                    <p className="font-semibold">MakeMKV needs activation</p>
                    <p className="mt-1 leading-6 text-amber-100/85">DiscDock cannot activate MakeMKV or enter a license key for you. Any partial files are kept safely.</p>
                    <ol className="mt-3 list-decimal space-y-1 pl-5 leading-6 text-amber-100/85">
                      <li>Leave the disc inserted, then open MakeMKV.</li>
                      <li>If prompted, start its official 30-day evaluation; otherwise use Help → Register with a purchased key.</li>
                      <li>Let MakeMKV open the disc once, then close it so the drive is released.</li>
                      <li>Return here and choose Retry.</li>
                    </ol>
                    <Button disabled={busy !== null} variant="outline" size="sm" className="mt-4 gap-2 border-amber-300/25 bg-amber-50/5 text-amber-50 hover:bg-amber-50/10" onClick={() => void controls.openMakeMKV()}><KeyRound className="size-3.5" /> {busy === "open-makemkv" ? "Opening…" : "Open MakeMKV"}</Button>
                  </div>
                </div>
              ) : selected.error_message && <div className="flex gap-3 rounded-xl border border-amber-400/15 bg-amber-400/8 p-4 text-sm leading-6 text-amber-100"><AlertTriangle className="mt-1 size-4 shrink-0" /><p className="whitespace-pre-wrap">{selected.error_message}</p></div>}
              {selected.tracks && selected.tracks.length > 0 && (
                <div><h3 className="mb-3 text-sm font-semibold">Disc titles</h3><div className="divide-y divide-white/7 rounded-xl border border-white/8">{selected.tracks.map((track) => <div key={track.source_id} className="flex items-center justify-between gap-4 px-4 py-3"><div><p className="text-sm font-medium">{track.name || `Title ${track.source_id}`}</p><p className="mt-1 text-xs text-muted-foreground">{Math.floor(track.duration_seconds / 60)} min · {track.chapters} chapters</p></div><span className="text-xs text-muted-foreground">{formatBytes(track.size_bytes)}</span></div>)}</div></div>
              )}
              <div className="flex flex-wrap gap-2">
                {selected.output_path && <Button className="gap-2 bg-primary text-primary-foreground" onClick={() => void controls.openOutput(selected.id)}><FolderOpen className="size-4" /> Open output</Button>}
                {selected.recoverable && <Button variant="outline" className="gap-2 border-white/10" onClick={() => void controls.retry(selected.id)}><RefreshCw className="size-4" /> Retry</Button>}
                <Button variant="outline" className="gap-2 border-white/10" asChild><a href={apiUrl(`/api/v1/jobs/${selected.id}/log`)} target="_blank" rel="noreferrer"><FileText className="size-4" /> View log</a></Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function Detail({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return <div className={wide ? "sm:col-span-2" : ""}><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 break-all font-medium capitalize">{value}</p></div>;
}

function formatRepairTime(seconds: number): string {
  const safe = Math.max(0, seconds);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = (safe % 60).toFixed(3).padStart(6, "0");
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${remainder}`;
}

const TREATMENT_LABELS: Record<DamageTreatment, string> = {
  loading_screen: "Loading screen",
  skipped: "Frozen picture",
  brief_glitch: "Brief glitch",
  ai_frames: "AI frames",
};

const TREATMENT_STYLES: Record<DamageTreatment, string> = {
  loading_screen: "bg-primary/12 text-primary",
  skipped: "bg-amber-400/12 text-amber-200",
  brief_glitch: "bg-white/7 text-muted-foreground",
  ai_frames: "bg-sky-400/12 text-sky-200",
};

function needsLoadingScreen(moment: DamageMoment): boolean {
  return moment.duration_seconds >= 2 && (moment.treatment ?? "skipped") === "skipped";
}

/** Movie time as HH:MM:SS; ends round up so they point past the damage, like the loading screen. */
function formatClock(seconds: number, roundUp = false): string {
  const total = Math.max(0, roundUp ? Math.ceil(seconds - 1e-6) : Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return [hours, minutes, total % 60].map((part) => String(part).padStart(2, "0")).join(":");
}

function formatLength(seconds: number): string {
  const total = Math.round(Math.max(0, seconds));
  if (total < 60) return `${Math.max(1, total)} s`;
  return `${Math.floor(total / 60)} min ${String(total % 60).padStart(2, "0")} s`;
}

function damageSummary(job: Job): string {
  const moments = damageMomentsFromJob(job).filter((moment) => moment.duration_seconds >= 1);
  if (moments.length === 0) return "";
  const shown = moments.slice(0, 3).map((moment) => `${formatClock(moment.start_seconds)}–${formatClock(moment.end_seconds, true)}`);
  return `Disc damage at ${shown.join(", ")}${moments.length > 3 ? ` +${moments.length - 3} more` : ""}`;
}
