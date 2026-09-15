import { Badge } from "@/components/ui/badge";

const STATE_LABELS: Record<string, string> = {
  detected: "Detected", inspecting: "Inspecting", identifying: "Identifying",
  awaiting_input: "Waiting for you", awaiting_repair: "Review AI repair", awaiting_album: "Waiting for the album",queued: "Queued", ripping: "Ripping",
  ripped: "Ripped", verifying: "Verifying", transcoding: "Transcoding",
  finalizing: "Finalizing", ejecting: "Ejecting", completed: "Complete",
  cancelling: "Stopping", cancelled: "Cancelled", interrupted: "Interrupted",
  blocked: "Needs attention", failed: "Failed",
};

export function StateBadge({ state }: { state: string }) {
  const color = state === "completed"
    ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200"
    : ["failed", "interrupted", "blocked"].includes(state)
      ? "border-amber-400/25 bg-amber-400/10 text-amber-200"
      : ["cancelled", "cancelling"].includes(state)
        ? "border-white/10 bg-white/5 text-muted-foreground"
        : "border-primary/25 bg-primary/10 text-primary";
  return <Badge variant="outline" className={`shrink-0 whitespace-nowrap ${color}`}>{STATE_LABELS[state] ?? state}</Badge>;
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function titleFor(job: { title: string; disc_label: string; year: string }): string {
  const title = job.title || job.disc_label || "Unidentified disc";
  return job.year && !title.includes(job.year) ? `${title} (${job.year.slice(0, 4)})` : title;
}
