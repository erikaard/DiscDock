"use client";

import { useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Disc3,
  History,
  LayoutDashboard,
  Library,
  MoreHorizontal,
  RefreshCw,
  Settings,
  TerminalSquare,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DashboardView } from "@/components/discdock/dashboard-view";
import { HistoryView } from "@/components/discdock/history-view";
import { LogsView } from "@/components/discdock/logs-view";
import { NotificationsPopover } from "@/components/discdock/notifications-popover";
import { SettingsView } from "@/components/discdock/settings-view";
import { useDiscDock } from "@/hooks/use-discdock";
import { useDiscDockWebMcp } from "@/hooks/use-webmcp";

type View = "dashboard" | "history" | "library" | "logs" | "settings";

const NAVIGATION: Array<{ id: View; label: string; icon: LucideIcon }> = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "history", label: "History", icon: History },
  { id: "library", label: "Library", icon: Library },
  { id: "logs", label: "Logs", icon: TerminalSquare },
];

const TITLES: Record<View, { eyebrow: string; title: string }> = {
  dashboard: { eyebrow: "Windows rip station", title: "Ripping dashboard" },
  history: { eyebrow: "Every disc attempt", title: "Job history" },
  library: { eyebrow: "Validated outputs", title: "Local library" },
  logs: { eyebrow: "Troubleshooting", title: "Logs and diagnostics" },
  settings: { eyebrow: "Native Windows setup", title: "DiscDock settings" },
};

export default function Home() {
  const [view, setView] = useState<View>("dashboard");
  const { data, loading, error, busy, controls } = useDiscDock();
  useDiscDockWebMcp(data, controls);
  const status = error
    ? { label: "Service offline", color: "bg-red-400", className: "border-red-400/25 bg-red-400/8 text-red-200" }
    : data?.health.ok
      ? { label: data.health.automatic_ripping ? "Automatic ripping on" : "Ready for a disc", color: "bg-emerald-400", className: "border-emerald-400/25 bg-emerald-400/8 text-emerald-200" }
      : { label: "Setup required", color: "bg-amber-400", className: "border-amber-400/25 bg-amber-400/8 text-amber-200" };

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="mx-auto flex min-h-screen max-w-[1540px]">
        <aside className="hidden w-[224px] shrink-0 border-r border-white/8 px-4 py-5 lg:flex lg:flex-col">
          <Brand />
          <nav aria-label="Main navigation" className="space-y-1">
            {NAVIGATION.map((item) => <NavItem key={item.id} {...item} active={view === item.id} onClick={() => setView(item.id)} />)}
          </nav>
          <div className="mt-auto space-y-1">
            <NavItem id="settings" label="Settings" icon={Settings} active={view === "settings"} onClick={() => setView("settings")} />
            <div className="mt-4 rounded-xl border border-white/8 bg-white/[0.025] p-3.5">
              <div className="mb-2 flex items-center gap-2 text-sm font-medium"><span className={`size-2 rounded-full ${status.color} shadow-[0_0_12px_currentColor]`} />{status.label}</div>
              <p className="text-xs leading-5 text-muted-foreground">Local only · Windows native</p>
            </div>
          </div>
        </aside>

        <section className="min-w-0 flex-1 pb-20 lg:pb-0">
          <header className="flex min-h-[74px] items-center justify-between gap-4 border-b border-white/8 px-5 py-3 sm:px-8">
            <div className="flex items-center gap-3 lg:hidden"><div className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground"><Disc3 className="size-5" /></div><div><p className="font-semibold">DiscDock</p><p className="text-xs text-muted-foreground">{TITLES[view].title}</p></div></div>
            <div className="hidden lg:block"><p className="text-sm text-muted-foreground">{TITLES[view].eyebrow}</p><h1 className="text-lg font-semibold tracking-tight">{TITLES[view].title}</h1></div>
            <div className="flex items-center gap-2">
              <Badge variant="outline" className={`h-8 gap-2 px-3 ${status.className}`}><span className={`size-1.5 rounded-full ${status.color}`} /> <span className="hidden sm:inline">{status.label}</span></Badge>
              <NotificationsPopover notifications={data?.notifications ?? []} busy={busy} controls={controls} />
              <Button disabled={busy !== null} aria-label="Refresh DiscDock" variant="ghost" size="icon" className="text-muted-foreground" onClick={() => void controls.refresh()}><RefreshCw className={busy === "refresh" ? "animate-spin" : ""} /></Button>
              <Button aria-label="Open settings" variant="ghost" size="icon" className="text-muted-foreground" onClick={() => setView("settings")}><MoreHorizontal /></Button>
            </div>
          </header>

          <div className="p-5 sm:p-8">
            {view === "dashboard" && <DashboardView data={data} loading={loading} error={error} busy={busy} controls={controls} openSettings={() => setView("settings")} />}
            {view === "history" && <HistoryView jobs={data?.jobs ?? []} busy={busy} controls={controls} />}
            {view === "library" && <HistoryView jobs={data?.jobs ?? []} busy={busy} controls={controls} libraryOnly />}
            {view === "logs" && <LogsView jobs={data?.jobs ?? []} />}
            {view === "settings" && data && <SettingsView key={JSON.stringify(data.settings)} settings={data.settings} health={data.health} busy={busy} controls={controls} />}
            {view === "settings" && !data && <div className="rounded-2xl border border-white/8 bg-card p-10 text-center text-sm text-muted-foreground">Connect to the local DiscDock service to edit settings.</div>}
          </div>
        </section>
      </div>

      <nav aria-label="Mobile navigation" className="fixed inset-x-3 bottom-3 z-40 grid grid-cols-5 rounded-2xl border border-white/10 bg-[#0a1518]/95 p-1.5 shadow-2xl backdrop-blur lg:hidden">
        {[...NAVIGATION.slice(0, 3), { id: "logs" as View, label: "Logs", icon: TerminalSquare }, { id: "settings" as View, label: "Settings", icon: Settings }].map((item) => {
          const Icon = item.icon;
          return <button key={item.id} type="button" aria-current={view === item.id ? "page" : undefined} onClick={() => setView(item.id)} className={`flex min-w-0 flex-col items-center gap-1 rounded-xl px-1 py-2 text-[10px] transition-colors ${view === item.id ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-white/5 hover:text-foreground"}`}><Icon className="size-4" /><span className="truncate">{item.label}</span></button>;
        })}
      </nav>
    </main>
  );
}

function Brand() {
  return <div className="flex items-center gap-3 px-2 pb-8"><div className="grid size-10 place-items-center rounded-xl bg-primary text-primary-foreground shadow-[0_0_30px_rgb(64_221_198/20%)]"><Disc3 className="size-5" strokeWidth={2.2} /></div><div><p className="text-[1.05rem] font-semibold tracking-tight">DiscDock</p><p className="text-xs text-muted-foreground">Windows rip station</p></div></div>;
}

function NavItem({ label, icon: Icon, active, onClick }: { id: View; label: string; icon: LucideIcon; active: boolean; onClick: () => void }) {
  return <button aria-current={active ? "page" : undefined} className={`flex h-10 w-full items-center gap-3 rounded-lg px-3 text-left text-sm transition-colors ${active ? "bg-primary/10 font-medium text-primary" : "text-muted-foreground hover:bg-white/5 hover:text-foreground"}`} type="button" onClick={onClick}><Icon className="size-[18px]" strokeWidth={1.8} />{label}</button>;
}
