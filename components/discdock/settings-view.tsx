"use client";

import { useMemo, useState } from "react";
import {
  BellRing,
  CheckCircle2,
  Database,
  FolderCog,
  KeyRound,
  LifeBuoy,
  Loader2,
  Save,
  ShieldCheck,
  Sparkles,
  Wrench,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import type { DiscDockControls } from "@/hooks/use-discdock";
import type { Health, Settings } from "@/lib/discdock-api";

type Draft = {
  auto_rip: boolean;
  auto_eject: boolean;
  prevent_sleep: boolean;
  skip_transcode: boolean;
  keep_raw_after_transcode: boolean;
  main_feature: boolean;
  extras: boolean;
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
  rip_mode: string;
  data_root: string;
};

const pickDraft = (settings: Settings): Draft => ({
  auto_rip: settings.auto_rip,
  auto_eject: settings.auto_eject,
  prevent_sleep: settings.prevent_sleep,
  skip_transcode: settings.skip_transcode,
  keep_raw_after_transcode: settings.keep_raw_after_transcode,
  main_feature: settings.main_feature,
  extras: settings.extras,
  min_length_seconds: settings.min_length_seconds,
  max_length_seconds: settings.max_length_seconds,
  duplicate_policy: settings.duplicate_policy,
  metadata_provider: settings.metadata_provider,
  omdb_enabled: settings.omdb_enabled,
  ai_repair_enabled: settings.ai_repair_enabled,
  ai_repair_model: settings.ai_repair_model,
  ai_repair_quality: settings.ai_repair_quality,
  ai_repair_keyframes_per_second: settings.ai_repair_keyframes_per_second,
  ai_repair_cost_limit_usd: settings.ai_repair_cost_limit_usd,
  rescue_extra_minutes: settings.rescue_extra_minutes ?? 30,
  damaged_disc_action: settings.damaged_disc_action ?? "ask",
  damage_placeholder: settings.damage_placeholder ?? "ask",
  cd_read_offset: settings.cd_read_offset ?? 0,
  notifications_enabled: settings.notifications_enabled,
  rip_mode: settings.rip_mode,
  data_root: settings.data_root,
});

export function SettingsView({ settings, health, busy, controls }: {
  settings: Settings;
  health: Health;
  busy: string | null;
  controls: DiscDockControls;
}) {
  const [draft, setDraft] = useState<Draft>(() => pickDraft(settings));
  const [omdbKey, setOmdbKey] = useState("");
  const [openAiKey, setOpenAiKey] = useState("");
  const [appriseUrls, setAppriseUrls] = useState("");

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(pickDraft(settings)) || Boolean(omdbKey.trim()) || Boolean(openAiKey.trim()) || Boolean(appriseUrls.trim()),
    [appriseUrls, draft, omdbKey, openAiKey, settings],
  );

  const update = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const save = async () => {
    const secrets: Record<string, string> = {};
    if (omdbKey.trim()) secrets.omdb_api_key = omdbKey.trim();
    if (openAiKey.trim()) secrets.openai_api_key = openAiKey.trim();
    if (appriseUrls.trim()) secrets.apprise_urls = appriseUrls.trim();
    const result = await controls.saveSettings(draft, secrets);
    if (result) {
      setOmdbKey("");
      setOpenAiKey("");
      setAppriseUrls("");
    }
  };
  const testOmdb = async () => {
    const result = await controls.testOmdb(omdbKey.trim());
    if (result?.ok) toast.success(result.message || "OMDb connection works");
  };
  const testOpenAi = async () => {
    const result = await controls.testOpenAi(openAiKey.trim());
    if (result?.ok) toast.success(result.message || "OpenAI connection works");
  };

  return (
    <div className="space-y-6 pb-24 lg:pb-0">
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Local configuration · DiscDock {health.version}</p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">Settings</h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">Everything stays on this Windows computer. API keys are encrypted for your Windows account.</p>
        </div>
        <Button disabled={!dirty || busy !== null} className="gap-2 bg-primary text-primary-foreground" onClick={() => void save()}>
          {busy === "save-settings" ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
          Save changes
        </Button>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Section icon={FolderCog} eyebrow="Workflow" title="What happens after insertion" description="Choose how automatic the ripping station should be.">
          <ToggleRow label="Rip automatically" detail="Start when Windows reports a newly inserted disc." checked={draft.auto_rip} onChange={(value) => update("auto_rip", value)} />
          <ToggleRow label="Eject when finished" detail="Open the tray only after output validation succeeds." checked={draft.auto_eject} onChange={(value) => update("auto_eject", value)} />
          <ToggleRow label="Keep the computer awake" detail="Prevents sleep while a scan, rip, or conversion is active." checked={draft.prevent_sleep} onChange={(value) => update("prevent_sleep", value)} />
          <ToggleRow label="Keep original MKV files" detail="Retain raw output after an optional HandBrake conversion." checked={draft.keep_raw_after_transcode} onChange={(value) => update("keep_raw_after_transcode", value)} />
          <div className="grid gap-4 pt-2 sm:grid-cols-2">
            <Field label="Duplicate handling">
              <Select value={draft.duplicate_policy} onValueChange={(value) => update("duplicate_policy", value)}>
                <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="ask">Ask me</SelectItem><SelectItem value="skip">Skip duplicate</SelectItem><SelectItem value="replace">Replace output</SelectItem><SelectItem value="keep_both">Keep both</SelectItem></SelectContent>
              </Select>
            </Field>
            <Field label="Video output">
              <Select value={draft.skip_transcode ? "mkv" : "handbrake"} onValueChange={(value) => update("skip_transcode", value === "mkv")}>
                <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="mkv">Original-quality MKV</SelectItem><SelectItem value="handbrake">Smaller HandBrake file</SelectItem></SelectContent>
              </Select>
            </Field>
          </div>
        </Section>

        <Section icon={Wrench} eyebrow="Disc selection" title="Titles and extras" description="These defaults can still be overridden for a manual job.">
          <ToggleRow label="Select main feature" detail="Rip exactly one likely feature, using OMDb runtime when available and filtering duplicate angles." checked={draft.main_feature} onChange={(value) => setDraft((current) => ({ ...current, main_feature: value, ...(value ? { extras: false } : {}) }))} />
          <ToggleRow label="Include extras and episodes" detail="Keep every title that passes the duration filter instead of choosing one feature." checked={draft.extras && !draft.main_feature} onChange={(value) => setDraft((current) => ({ ...current, extras: value, ...(value ? { main_feature: false } : {}) }))} />
          <div className="grid gap-4 pt-2 sm:grid-cols-2">
            <Field label="Minimum title length (minutes)">
              <Input type="number" min={0} max={1440} value={Math.round(draft.min_length_seconds / 60)} onChange={(event) => update("min_length_seconds", Math.max(0, Number(event.target.value) * 60))} className="border-white/10 bg-white/[0.025]" />
            </Field>
            <Field label="Maximum title length (minutes)">
              <Input type="number" min={1} max={2880} value={Math.round(draft.max_length_seconds / 60)} onChange={(event) => update("max_length_seconds", Math.max(60, Number(event.target.value) * 60))} className="border-white/10 bg-white/[0.025]" />
            </Field>
          </div>
        </Section>

        <Section icon={KeyRound} eyebrow="Metadata" title="OMDb movie matching" description="DiscDock uses the disc label as a search hint and lets you correct uncertain matches.">
          <ToggleRow label="Use OMDb" detail="Fetch title, year, plot, type, and poster metadata." checked={draft.omdb_enabled} onChange={(value) => update("omdb_enabled", value)} />
          <Field label="OMDb API key" hint={settings.secrets?.omdb_api_key ? "A key is already securely saved. Enter a value only to replace it." : "Get a free key from omdbapi.com, then activate it from the email they send."}>
            <div className="flex gap-2">
              <Input type="password" autoComplete="off" value={omdbKey} onChange={(event) => setOmdbKey(event.target.value)} placeholder={settings.secrets?.omdb_api_key ? "•••••••••••• (configured)" : "Paste your key"} className="border-white/10 bg-white/[0.025]" />
              <Button disabled={busy !== null || (!omdbKey.trim() && !settings.secrets?.omdb_api_key)} variant="outline" className="shrink-0 border-white/10" onClick={() => void testOmdb()}>
                {busy === "test-omdb" ? <Loader2 className="size-4 animate-spin" /> : "Test"}
              </Button>
            </div>
          </Field>
          <div className="flex items-center gap-2 rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 text-sm">
            {settings.secrets?.omdb_api_key ? <CheckCircle2 className="size-4 text-emerald-300" /> : <XCircle className="size-4 text-amber-300" />}
            <span className="text-muted-foreground">OMDb key</span><span className="ml-auto font-medium">{settings.secrets?.omdb_api_key ? "Configured" : "Not configured"}</span>
          </div>
        </Section>

        <Section icon={LifeBuoy} eyebrow="Damaged discs" title="Best-effort recovery" description="When a DVD or Blu-ray has unreadable blocks, DiscDock copies everything it can read, skips what the drive cannot read, and still finishes the movie.">
          <Field label="When a disc has unreadable blocks" hint="MakeMKV can spend hours retrying a scratched disc. Automatic best effort switches to the damaged-disc rescue as soon as MakeMKV reports an unreadable block, so an unattended rip still finishes.">
            <Select value={draft.damaged_disc_action} onValueChange={(value) => update("damaged_disc_action", value)}>
              <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="ask">Ask me · show recovery choices</SelectItem><SelectItem value="best_effort">Continue with best effort automatically</SelectItem></SelectContent>
            </Select>
          </Field>
          <Field label="Extra time for re-reading damaged areas" hint="After the first pass, DiscDock retries the skipped spots and stops early when retrying stops recovering data. This is the longest it keeps trying. You can skip the retries at any time.">
            <Select value={String(draft.rescue_extra_minutes)} onValueChange={(value) => update("rescue_extra_minutes", Number(value))}>
              <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
              <SelectContent>{Array.from(new Set([0, 15, 30, 60, 120, 240, 480, draft.rescue_extra_minutes])).sort((a, b) => a - b).map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{minutes === 0 ? "None · fastest" : minutes < 60 ? `${minutes} minutes` : `${minutes / 60} ${minutes === 60 ? "hour" : "hours"}`}{minutes === 30 ? " · recommended" : ""}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
          <Field label="Where the movie is damaged" hint="For damage of 2 seconds or more after a best-effort recovery. With Ask, the movie is finished as it was read and the dashboard lets you keep it or add loading screens. A loading screen shows the time the movie continues and counts down to it, and a chapter mark lets you skip it. Only a few seconds around each damaged moment are encoded again, and the movie without loading screens is kept next to it.">
            <Select value={draft.damage_placeholder} onValueChange={(value) => update("damage_placeholder", value)}>
              <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="ask">Ask me · keep the movie as read or add loading screens</SelectItem><SelectItem value="loading_screen">Always show a loading screen with the resume time</SelectItem><SelectItem value="none">Always leave the movie as it is · frozen picture</SelectItem></SelectContent>
            </Select>
          </Field>
          <p className="rounded-xl border border-amber-400/15 bg-amber-400/7 p-3 text-xs leading-5 text-amber-100">Parts the drive cannot read are left out. Short damage shows as a brief freeze or blocky picture, longer damage as the loading screen above. AI repair can replace short damaged moments. The Library lists every damaged time for each movie.</p>
        </Section>

        <Section icon={Sparkles} eyebrow="Damaged discs" title="AI frame reconstruction" description="Replace short damaged moments with frames generated by OpenAI. Nothing is sent or charged until you approve an estimate.">
          <ToggleRow label="Offer AI repair" detail="For DVDs and Blu-rays, when there is intact video right before and after the damage." checked={draft.ai_repair_enabled} onChange={(value) => update("ai_repair_enabled", value)} />
          <Field label="OpenAI API key" hint={settings.secrets?.openai_api_key ? "Securely saved with Windows encryption. Enter a value only to replace it." : "Create a project key at platform.openai.com. It stays encrypted on this PC and is never written to logs."}>
            <div className="flex gap-2">
              <Input type="password" autoComplete="off" value={openAiKey} onChange={(event) => setOpenAiKey(event.target.value)} placeholder={settings.secrets?.openai_api_key ? "•••••••••••• (configured)" : "Paste your project API key"} className="border-white/10 bg-white/[0.025]" />
              <Button disabled={busy !== null || (!openAiKey.trim() && !settings.secrets?.openai_api_key)} variant="outline" className="shrink-0 border-white/10" onClick={() => void testOpenAi()}>
                {busy === "test-openai" ? <Loader2 className="size-4 animate-spin" /> : "Test"}
              </Button>
            </div>
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Reconstruction model">
              <Select value={draft.ai_repair_model} onValueChange={(value) => update("ai_repair_model", value)}>
                <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="gpt-image-2.5-sunburst">Sunburst · precise</SelectItem><SelectItem value="gpt-image-2.5-flare">Flare · faster</SelectItem></SelectContent>
              </Select>
            </Field>
            <Field label="Quality">
              <Select value={draft.ai_repair_quality} onValueChange={(value) => update("ai_repair_quality", value)}>
                <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="low">Low · recommended</SelectItem><SelectItem value="medium">Medium · higher cost</SelectItem></SelectContent>
              </Select>
            </Field>
            <Field label="AI keyframes per missing second" hint="DiscDock fills in the frames between the generated keyframes.">
              <Select value={String(draft.ai_repair_keyframes_per_second)} onValueChange={(value) => update("ai_repair_keyframes_per_second", Number(value))}>
                <SelectTrigger className="w-full border-white/10 bg-white/[0.025]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="1">1 · economical</SelectItem><SelectItem value="2">2 · balanced</SelectItem><SelectItem value="4">4 · smoother</SelectItem></SelectContent>
              </Select>
            </Field>
            <Field label="Maximum approved cost per repair (USD)" hint="DiscDock still shows a separate estimate and asks before making any paid request.">
              <Input type="number" min={0.25} max={100} step={0.25} value={draft.ai_repair_cost_limit_usd} onChange={(event) => update("ai_repair_cost_limit_usd", Math.max(0.25, Number(event.target.value)))} className="border-white/10 bg-white/[0.025]" />
            </Field>
          </div>
          <div className="flex items-center gap-2 rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3 text-sm">
            {settings.secrets?.openai_api_key ? <CheckCircle2 className="size-4 text-emerald-300" /> : <XCircle className="size-4 text-amber-300" />}
            <span className="text-muted-foreground">OpenAI project key</span><span className="ml-auto font-medium">{settings.secrets?.openai_api_key ? "Configured" : "Not configured"}</span>
          </div>
          <p className="rounded-xl border border-amber-400/15 bg-amber-400/7 p-3 text-xs leading-5 text-amber-100">Generated frames are a best guess, not the original picture, and every repair is listed in the job details. Audio is left as it is and may drop out briefly.</p>
        </Section>

        <Section icon={BellRing} eyebrow="Notifications" title="Completion alerts" description="Apprise supports Discord, Telegram, email, Pushover, and many other services.">
          <ToggleRow label="Send notifications" detail="Notify on completed jobs, failures, and jobs needing attention." checked={draft.notifications_enabled} onChange={(value) => update("notifications_enabled", value)} />
          <Field label="Apprise URLs" hint={settings.secrets?.apprise_urls ? "Notification destinations are already saved. Enter URLs only to replace them." : "One URL per line. These are encrypted and never displayed again."}>
            <Textarea value={appriseUrls} onChange={(event) => setAppriseUrls(event.target.value)} placeholder={settings.secrets?.apprise_urls ? "Configured, leave blank to keep" : "discord://…\ntgram://…"} className="min-h-24 border-white/10 bg-white/[0.025] font-mono text-xs" />
          </Field>
          <Button disabled={busy !== null || !settings.secrets?.apprise_urls} variant="outline" className="gap-2 border-white/10" onClick={() => void controls.testNotification()}>
            {busy === "test-notification" ? <Loader2 className="size-4 animate-spin" /> : <BellRing className="size-4" />} Send a test
          </Button>
        </Section>

        <Section icon={Database} eyebrow="Storage" title="Local output folders" description="You can copy finished media to your NAS whenever you are ready.">
          <Field label="DiscDock data folder" hint="Raw files, completed media, logs, and the job database live below this folder.">
            <Input value={draft.data_root} onChange={(event) => update("data_root", event.target.value)} className="border-white/10 bg-white/[0.025] font-mono text-xs" />
          </Field>
          <DirectoryRows directories={settings.directories} />
        </Section>

        <Section icon={ShieldCheck} eyebrow="Windows tools" title="Native media engines" description="DiscDock runs these programs directly on Windows, without Docker or WSL.">
          <ToolRow label="MakeMKV" ready={health.tools.makemkv} required />
          <ToolRow label="FFmpeg / FFprobe" ready={Boolean(health.tools.ffmpeg && health.tools.ffprobe)} required />
          <ToolRow label="VLC preview" ready={health.tools.vlc} />
          <ToolRow label="HandBrake conversion" ready={health.tools.handbrake} />
          <ToolRow label="cyanrip audio CDs" ready={health.tools.cyanrip} />
          <Field label="CD drive read offset (samples)" hint="Every CD drive reads audio a few samples early or late. Enter your drive's offset from the AccurateRip drive offset list, the same number EAC uses, so the rip can be checked against AccurateRip. With 0 the CD is still ripped, shifted by a fraction of a millisecond.">
            <Input type="number" min={-5000} max={5000} step={1} value={draft.cd_read_offset} onChange={(event) => update("cd_read_offset", Math.max(-5000, Math.min(5000, Math.trunc(Number(event.target.value) || 0))))} className="border-white/10 bg-white/[0.025]" />
          </Field>
          {(!health.tools.handbrake || !health.tools.cyanrip) && <p className="rounded-xl border border-amber-400/15 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">Video ripping is ready. Install the optional tools to enable compressed HandBrake output and tagged audio-CD ripping.</p>}
        </Section>
      </div>
    </div>
  );
}

function Section({ icon: Icon, eyebrow, title, description, children }: {
  icon: typeof Wrench;
  eyebrow: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="border-white/8 bg-card shadow-none">
      <CardHeader>
        <div className="flex items-start gap-3">
          <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary/8 text-primary"><Icon className="size-4.5" /></div>
          <div><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">{eyebrow}</p><CardTitle className="mt-1.5 text-lg">{title}</CardTitle><CardDescription className="mt-1 leading-5">{description}</CardDescription></div>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">{children}</CardContent>
    </Card>
  );
}

function ToggleRow({ label, detail, checked, onChange }: { label: string; detail: string; checked: boolean; onChange: (value: boolean) => void }) {
  return <div className="flex items-center justify-between gap-5"><div><Label className="text-sm font-medium">{label}</Label><p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p></div><Switch checked={checked} onCheckedChange={onChange} /></div>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <div className="space-y-2"><Label>{label}</Label>{children}{hint && <p className="text-xs leading-5 text-muted-foreground">{hint}</p>}</div>;
}

function ToolRow({ label, ready, required = false }: { label: string; ready: boolean; required?: boolean }) {
  return <div className="flex items-center justify-between gap-4 rounded-xl border border-white/8 bg-white/[0.02] px-4 py-3"><div className="flex items-center gap-2.5">{ready ? <CheckCircle2 className="size-4 text-emerald-300" /> : <XCircle className={`size-4 ${required ? "text-red-300" : "text-amber-300"}`} />}<span className="text-sm">{label}</span></div><Badge variant="outline" className={ready ? "border-emerald-400/20 bg-emerald-400/8 text-emerald-200" : required ? "border-red-400/20 bg-red-400/8 text-red-200" : "border-amber-400/20 bg-amber-400/8 text-amber-200"}>{ready ? "Ready" : required ? "Required" : "Optional"}</Badge></div>;
}

function DirectoryRows({ directories }: { directories: Record<string, string> }) {
  return <div className="divide-y divide-white/7 rounded-xl border border-white/8 bg-white/[0.02]">{["raw", "completed", "music", "failed"].map((key) => <div key={key} className="flex min-w-0 items-center gap-3 px-4 py-3"><span className="w-20 shrink-0 text-xs font-medium capitalize text-muted-foreground">{key}</span><code className="truncate text-xs">{directories[key] ?? "—"}</code></div>)}</div>;
}
