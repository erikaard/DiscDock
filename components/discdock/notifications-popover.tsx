"use client";

import { AlertTriangle, Bell, CheckCircle2, Info, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { DiscDockNotification } from "@/lib/discdock-api";
import type { DiscDockControls } from "@/hooks/use-discdock";
import { formatDate } from "./status";

export function NotificationsPopover({ notifications, busy, controls }: {
  notifications: DiscDockNotification[];
  busy: string | null;
  controls: DiscDockControls;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          aria-label={notifications.length ? `${notifications.length} recent notifications` : "No recent notifications"}
          variant="ghost"
          size="icon"
          className="relative text-muted-foreground"
        >
          <Bell />
          {notifications.length > 0 && (
            <span className="absolute right-1 top-1 min-w-4 rounded-full bg-primary px-1 text-center text-[9px] font-bold leading-4 text-primary-foreground">
              {notifications.length > 9 ? "9+" : notifications.length}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={10}
        className="w-[min(24rem,calc(100vw-2rem))] border-white/10 bg-[#0c171a] p-0 shadow-2xl"
      >
        <PopoverHeader className="border-b border-white/8 px-4 py-3">
          <PopoverTitle>Notifications</PopoverTitle>
          <PopoverDescription>Recent completion, failure, and attention messages.</PopoverDescription>
        </PopoverHeader>
        {notifications.length === 0 ? (
          <div className="px-5 py-8 text-center">
            <Bell className="mx-auto size-5 text-muted-foreground" />
            <p className="mt-3 text-sm font-medium">Nothing new</p>
            <p className="mt-1 text-xs text-muted-foreground">DiscDock alerts will appear here.</p>
          </div>
        ) : (
          <div className="max-h-[min(28rem,70vh)] divide-y divide-white/7 overflow-y-auto">
            {notifications.map((notification) => {
              const Icon = notification.event_type === "completed"
                ? CheckCircle2
                : notification.event_type === "failed" || notification.state === "failed"
                  ? AlertTriangle
                  : Info;
              const iconClass = notification.event_type === "completed"
                ? "bg-emerald-400/10 text-emerald-300"
                : notification.event_type === "failed" || notification.state === "failed"
                  ? "bg-amber-400/10 text-amber-300"
                  : "bg-primary/10 text-primary";
              return (
                <div key={notification.id} className="flex gap-3 px-4 py-3">
                  <div className={`mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg ${iconClass}`}>
                    <Icon className="size-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium leading-5">{notification.title}</p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-muted-foreground">{notification.body}</p>
                    <p className="mt-1.5 text-[11px] text-muted-foreground/70">{formatDate(notification.created_at)}</p>
                  </div>
                  <Button
                    aria-label={`Dismiss ${notification.title}`}
                    title="Dismiss"
                    disabled={busy !== null}
                    variant="ghost"
                    size="icon"
                    className="size-7 shrink-0 text-muted-foreground"
                    onClick={() => void controls.dismissNotification(notification.id)}
                  >
                    <X className="size-3.5" />
                  </Button>
                </div>
              );
            })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
