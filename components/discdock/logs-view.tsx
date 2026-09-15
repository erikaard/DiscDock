"use client";

import { AlertTriangle, ExternalLink, FileText, TerminalSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Job } from "@/lib/discdock-api";
import { apiUrl } from "@/lib/discdock-api";
import { formatDate, StateBadge, titleFor } from "./status";

export function LogsView({ jobs }: { jobs: Job[] }) {
  return (
    <div className="space-y-6">
      <Card className="border-white/8 bg-card shadow-none">
        <CardHeader className="flex-row items-end justify-between gap-4">
          <div><p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Diagnostics</p><CardTitle className="mt-2 text-xl">Job logs</CardTitle></div>
          <Button variant="outline" className="gap-2 border-white/10" asChild><a href={apiUrl("/api/docs")} target="_blank" rel="noreferrer"><ExternalLink className="size-4" /> Service API</a></Button>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {jobs.length === 0 ? (
            <div className="flex min-h-[340px] flex-col items-center justify-center px-6 text-center"><div className="grid size-12 place-items-center rounded-2xl bg-primary/8 text-primary"><TerminalSquare className="size-5" /></div><h2 className="mt-5 text-lg font-semibold">No logs yet</h2><p className="mt-2 max-w-sm text-sm leading-6 text-muted-foreground">A separate plain-text log is created for every disc job.</p></div>
          ) : (
            <div className="divide-y divide-white/7">
              {jobs.map((job) => (
                <div key={job.id} className="flex flex-col gap-3 px-6 py-4 sm:flex-row sm:items-center">
                  <div className={`grid size-10 shrink-0 place-items-center rounded-xl ${job.error_message ? "bg-amber-400/10 text-amber-300" : "bg-white/5 text-muted-foreground"}`}>{job.error_message ? <AlertTriangle className="size-4" /> : <FileText className="size-4" />}</div>
                  <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{titleFor(job)}</p><p className="mt-1 truncate text-xs text-muted-foreground">{formatDate(job.updated_at)} · {job.status_detail}</p></div>
                  <StateBadge state={job.state} />
                  <Button variant="ghost" size="sm" className="gap-2" asChild><a href={apiUrl(`/api/v1/jobs/${job.id}/log`)} target="_blank" rel="noreferrer"><FileText className="size-4" /> Open log</a></Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
