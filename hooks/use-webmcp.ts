"use client";

import { useEffect } from "react";
import type { Bootstrap } from "@/lib/discdock-api";
import type { DiscDockControls } from "@/hooks/use-discdock";

type ToolDefinition = {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
  execute: (input: Record<string, unknown>) => unknown | Promise<unknown>;
};

type ModelContext = {
  registerTool: (tool: ToolDefinition, options?: { signal?: AbortSignal }) => void | Promise<void>;
};

export function useDiscDockWebMcp(data: Bootstrap | null, controls: DiscDockControls) {
  useEffect(() => {
    const context = (document as Document & { modelContext?: ModelContext }).modelContext;
    if (!context?.registerTool || !data) return;
    const lifecycle = new AbortController();
    const driveIds = data.drives.map((drive) => drive.id);
    const resolveDrive = (input: Record<string, unknown>) => {
      const requested = typeof input.driveId === "string" ? input.driveId : driveIds[0];
      if (!requested || !driveIds.includes(requested)) throw new Error("A valid optical drive is required");
      return requested;
    };

    const tools: ToolDefinition[] = [
      {
        name: "get_ripping_status",
        title: "Get ripping status",
        description: "Read the currently detected optical drives and active DiscDock jobs.",
        inputSchema: { type: "object", properties: {}, additionalProperties: false },
        annotations: { readOnlyHint: true, untrustedContentHint: false },
        execute: async () => ({ drives: data.drives, jobs: data.jobs.filter((job) => !["completed", "failed", "cancelled"].includes(job.state)) }),
      },
      {
        name: "start_disc_scan",
        title: "Start disc scan",
        description: "Inspect the loaded disc and begin the configured ripping workflow on a selected drive.",
        inputSchema: { type: "object", properties: { driveId: { type: "string", enum: driveIds }, manual: { type: "boolean" } }, additionalProperties: false },
        annotations: { readOnlyHint: false, untrustedContentHint: false },
        execute: async (input) => controls.scan(resolveDrive(input), input.manual === true),
      },
      {
        name: "eject_optical_disc",
        title: "Eject optical disc",
        description: "Safely eject a disc when the selected drive has no active job.",
        inputSchema: { type: "object", properties: { driveId: { type: "string", enum: driveIds } }, additionalProperties: false },
        annotations: { readOnlyHint: false, untrustedContentHint: false },
        execute: async (input) => controls.eject(resolveDrive(input)),
      },
    ];
    for (const tool of tools) {
      try {
        void Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(() => undefined);
      } catch {
        // Browsers without the current WebMCP proposal continue normally.
      }
    }
    return () => lifecycle.abort();
  }, [controls, data]);
}

