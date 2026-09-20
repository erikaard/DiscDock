"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AlbumRelease, api, Bootstrap, eventsUrl, Job, ManualAlbum, MetadataCandidate, Settings } from "@/lib/discdock-api";
import packageJson from "@/package.json";

// A dashboard left open during an update, or kept by the browser, belongs to the previous
// version. It loads the new one once, when the service answers with a different version.
function reloadForUpdatedService(serviceVersion: string | undefined) {
  if (process.env.NODE_ENV !== "production" || !serviceVersion || serviceVersion === packageJson.version) return;
  try {
    if (window.sessionStorage.getItem("discdock-reloaded-for") === serviceVersion) return;
    window.sessionStorage.setItem("discdock-reloaded-for", serviceVersion);
  } catch {
    return;
  }
  window.location.reload();
}

export function useDiscDock() {
  const [data, setData] = useState<Bootstrap | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const refreshTimer = useRef<number | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    try {
      const next = await api<Bootstrap>("/api/v1/bootstrap");
      reloadForUpdatedService(next.health?.version);
      setData(next);
      setError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "The DiscDock service is unavailable";
      setError(message);
      if (!quiet) toast.error(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const poll = window.setInterval(() => void refresh(true), 5000);
    const events = new EventSource(eventsUrl());
    events.onmessage = () => {
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => void refresh(true), 180);
    };
    const namedEvents = ["job.created", "job.updated", "drive.media_inserted", "drive.media_removed", "settings.updated", "notification.dismissed"];
    namedEvents.forEach((name) => events.addEventListener(name, events.onmessage as EventListener));
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(poll);
      events.close();
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
    };
  }, [refresh]);

  const action = useCallback(async <T,>(name: string, work: () => Promise<T>, success?: string): Promise<T | undefined> => {
    setBusy(name);
    try {
      const result = await work();
      if (success) toast.success(success);
      await refresh(true);
      return result;
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : "The action failed");
      return undefined;
    } finally {
      setBusy(null);
    }
  }, [refresh]);

  const controls = useMemo(() => ({
    refresh: () => action("refresh", () => refresh(), "Drive state refreshed"),
    scan: (driveId: string, manual = false, ripMethod: "normal" | "sector_rescue" = "normal") => action(ripMethod === "sector_rescue" ? "scan-rescue" : "scan", () => api<Job>(`/api/v1/drives/${driveId}/scan`, { method: "POST", body: JSON.stringify({ manual, rip_method: ripMethod }) }), ripMethod === "sector_rescue" ? "Sector-rescue rip started" : "Disc scan started"),
    eject: (driveId: string) => action("eject", () => api(`/api/v1/drives/${driveId}/eject`, { method: "POST", body: "{}" }), "Ejecting disc"),
    close: (driveId: string) => action("close", () => api(`/api/v1/drives/${driveId}/close`, { method: "POST", body: "{}" }), "Closing tray"),
    preview: (driveId: string) => action("preview", () => api(`/api/v1/drives/${driveId}/preview`, { method: "POST", body: "{}" }), "Opening disc in VLC"),
    cancel: (jobId: string) => action("cancel", () => api<Job>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST", body: "{}" }), "Job stopped safely"),
    retry: (jobId: string) => action("retry", () => api<Job>(`/api/v1/jobs/${jobId}/retry`, { method: "POST", body: "{}" }), "Job queued again"),
    recoverDamaged: (jobId: string) => action("recover-damaged", () => api<Job>(`/api/v1/jobs/${jobId}/recover-damaged`, { method: "POST", body: "{}" }), "Switching to best-effort recovery"),
    finishRescue: (jobId: string) => action("finish-rescue", () => api<Job>(`/api/v1/jobs/${jobId}/rescue/finish`, { method: "POST", body: "{}" }), "Continuing with the data rescued so far"),
    prepareAiRepair: (jobId: string) => action("prepare-ai-repair", () => api<Job>(`/api/v1/jobs/${jobId}/ai-repair/prepare`, { method: "POST", body: "{}" }), "Preparing the AI repair estimate — no API credit will be used"),
    applyAiRepair: (jobId: string, estimateId: string, acceptedMaxCostUsd: number) => action("apply-ai-repair", () => api<Job>(`/api/v1/jobs/${jobId}/ai-repair/apply`, { method: "POST", body: JSON.stringify({ estimate_id: estimateId, accepted_max_cost_usd: acceptedMaxCostUsd }) }), "AI repair approved"),
    keepWithoutAi: (jobId: string) => action("keep-without-ai", () => api<Job>(`/api/v1/jobs/${jobId}/ai-repair/keep`, { method: "POST", body: "{}" }), "Keeping the movie without AI frames"),
    addLoadingScreens: (jobId: string) => action("loading-screens", () => api<Job>(`/api/v1/jobs/${jobId}/loading-screens`, { method: "POST", body: "{}" }), "Adding loading screens to the movie"),
    keepDamagedMovie: (jobId: string) => action("keep-damaged-movie", () => api<Job>(`/api/v1/jobs/${jobId}/damage/keep`, { method: "POST", body: "{}" }), "Keeping the movie as it was read"),
    reviewDamage: (jobId: string) => action("review-damage", () => api<Job>(`/api/v1/jobs/${jobId}/damage/review`, { method: "POST", body: "{}" }), "Looking through the movie for broken parts — no API credit will be used"),
    addTitlesToCompleted: (jobId: string) => action("add-titles", () => api<Job>(`/api/v1/jobs/${jobId}/add-titles`, { method: "POST", body: "{}" }), "Choose the titles to add to the existing folder"),
    chooseRelease: (jobId: string, releaseId: string) => action("choose-release", () => api<Job>(`/api/v1/jobs/${jobId}/continue`, { method: "POST", body: JSON.stringify({ musicbrainz_release: releaseId }) }), releaseId === "none" ? "Ripping the CD without track names" : "Ripping the chosen release"),
    // Not an action: the album search shows MusicBrainz's answer, such as a 503, next to the search field.
    searchAlbums: (query: string, tracks = 0) => api<AlbumRelease[]>(`/api/v1/musicbrainz/search?q=${encodeURIComponent(query)}&tracks=${tracks}`),
    searchAlbumsByBarcode: (barcode: string, tracks = 0) => api<AlbumRelease[]>(`/api/v1/musicbrainz/barcode?code=${encodeURIComponent(barcode)}&tracks=${tracks}`),
    saveManualAlbum: (jobId: string, album: ManualAlbum) => action("manual-album", () => api<Job>(`/api/v1/jobs/${jobId}/album/manual`, { method: "POST", body: JSON.stringify(album) }), "Album saved. The tracks get these names and tags."),
    keepCdInStaging: (jobId: string, keep: boolean) => action("cd-staging", () => api<Job>(`/api/v1/jobs/${jobId}/album/staging`, { method: "POST", body: JSON.stringify({ keep }) }), keep ? "The CD waits in staging for its album when the rip is done" : "The CD finishes when the rip is done"),
    finishCd: (jobId: string, withoutAlbum: boolean) => action("finish-cd", () => api<Job>(`/api/v1/jobs/${jobId}/album/finish`, { method: "POST", body: JSON.stringify({ without_album: withoutAlbum }) }), "Finishing the CD"),
    uploadAlbumPhoto: (jobId: string, side: "front" | "front_original" | "back", photo: Blob) => action("album-photo", () => api<Job>(`/api/v1/jobs/${jobId}/album/photos/${side}`, { method: "PUT", body: photo, headers: { "Content-Type": photo.type || "image/jpeg" } })),
    removeAlbumPhoto: (jobId: string, side: "front" | "front_original" | "back") => action("album-photo", () => api<Job>(`/api/v1/jobs/${jobId}/album/photos/${side}`, { method: "DELETE" })),
    chooseAlbum: (jobId: string, release: AlbumRelease) => action("choose-album", () => api<Job>(`/api/v1/jobs/${jobId}/album`, { method: "POST", body: JSON.stringify(release) }), "Album chosen. The tracks get its names and tags when the rip is done."),
    continueJob: (jobId: string, selectedTitles: number[], patch: { title: string; year: string; media_kind: string; metadata?: MetadataCandidate }) => action("continue", async () => {
      await api<Job>(`/api/v1/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(patch) });
      return api<Job>(`/api/v1/jobs/${jobId}/continue`, { method: "POST", body: JSON.stringify({ selected_titles: selectedTitles }) });
    }, "Ripping selected titles"),
    backUpDisc: (jobId: string, patch: { title: string; year: string; media_kind: string }) => action("back-up-disc", async () => {
      await api<Job>(`/api/v1/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(patch) });
      return api<Job>(`/api/v1/jobs/${jobId}/continue`, { method: "POST", body: "{}" });
    }, "Backing up the disc"),
    updateMetadata: (jobId: string, patch: { title: string; year: string; media_kind: string; metadata: MetadataCandidate }) => action("update-metadata", () => api<Job>(`/api/v1/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(patch) }), "Title updated"),
    searchMetadata: (query: string, year = "") => action("metadata-search", () => api<MetadataCandidate[]>(`/api/v1/metadata/search?q=${encodeURIComponent(query)}&year=${encodeURIComponent(year)}`)),
    openOutput: (jobId: string) => action("open", () => api(`/api/v1/jobs/${jobId}/open-output`, { method: "POST", body: "{}" })),
    openMakeMKV: () => action("open-makemkv", () => api<{ ok: boolean; message: string }>("/api/v1/tools/makemkv/open", { method: "POST", body: "{}" }), "MakeMKV opened. Activate it, close it, then retry the job."),
    saveSettings: (values: Partial<Settings>, secrets: Record<string, string>) => action("save-settings", () => api<Settings>("/api/v1/settings", { method: "PUT", body: JSON.stringify({ values, secrets }) }), "Settings saved"),
    testOmdb: (key: string) => action("test-omdb", () => api<{ ok: boolean; message: string }>("/api/v1/settings/test/omdb", { method: "POST", body: JSON.stringify({ values: {}, secrets: key ? { omdb_api_key: key } : {} }) })),
    testOpenAi: (key: string) => action("test-openai", () => api<{ ok: boolean; message: string }>("/api/v1/settings/test/openai", { method: "POST", body: JSON.stringify({ values: {}, secrets: key ? { openai_api_key: key } : {} }) })),
    testNotification: () => action("test-notification", () => api("/api/v1/notifications/test", { method: "POST", body: "{}" }), "Test notification sent"),
    dismissNotification: (notificationId: number) => action("dismiss-notification", () => api<void>(`/api/v1/notifications/${notificationId}`, { method: "DELETE" })),
  }), [action, refresh]);

  return { data, loading, error, busy, controls };
}

export type DiscDockControls = ReturnType<typeof useDiscDock>["controls"];
