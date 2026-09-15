"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { Check, Crop, FlipHorizontal2, Loader2, RotateCcw, RotateCw, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Slider } from "@/components/ui/slider";
import {
  coverJpeg, coverSize, defaultEdit, fitWithin, loadPicture, mirrored, moveAll, moveCorner, moveSide, photoJpeg,
  preparePhoto, readPixels, renderCover, turnLeft, turnRight, wholePicture, type CoverEdit, type Point,
} from "@/lib/cover-image";

// The photo is shown at most this large while editing; the cover itself is made from the full photo.
const STAGE_SOURCE_MAX = 1200;
const PREVIEW_BOX = 240;
const CORNER_NAMES = ["top left", "top right", "bottom right", "bottom left"];
const SIDE_NAMES = ["top", "right", "bottom", "left"];

type Drag = { kind: "corner" | "side" | "area"; index: number; last: Point };

// Crops, straightens, turns and adjusts a photo of a CD case into its cover before it is saved.
export function CoverEditor({ photo, initial, keepPhoto, onCancel, onDone }: {
  photo: Blob;
  // How the cover was edited from this photo before, to go on from there.
  initial: CoverEdit | null;
  // Also hand back the photo as taken, so the cover can be edited again later.
  keepPhoto: boolean;
  onCancel: () => void;
  // Resolves true when the cover was saved; the editor then closes.
  onDone: (cover: Blob, edit: CoverEdit, photo: Blob | null) => Promise<boolean>;
}) {
  const [picture, setPicture] = useState<HTMLImageElement | null>(null);
  const [edit, setEdit] = useState<CoverEdit | null>(initial);
  const [problem, setProblem] = useState("");
  const [saving, setSaving] = useState(false);
  const area = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLCanvasElement>(null);
  const preview = useRef<HTMLCanvasElement>(null);
  const drag = useRef<Drag | null>(null);

  useEffect(() => {
    let current = true;
    loadPicture(photo)
      .then((loaded) => {
        if (!current) return;
        setPicture(loaded);
        setEdit((existing) => existing ?? defaultEdit(loaded.naturalWidth, loaded.naturalHeight));
      })
      .catch((error: unknown) => {
        if (current) setProblem(error instanceof Error ? error.message : "The photo could not be opened");
      });
    return () => {
      current = false;
    };
  }, [photo]);

  const turns = edit?.turns ?? 0;
  const straighten = edit?.straighten ?? 0;
  const mirror = edit?.mirror ?? false;
  const prepared = useMemo(() => {
    if (!picture) return null;
    const canvas = preparePhoto(picture, { turns, straighten, mirror }, STAGE_SOURCE_MAX);
    return { canvas, pixels: readPixels(canvas) };
  }, [picture, turns, straighten, mirror]);

  useEffect(() => {
    const canvas = stage.current;
    if (!canvas || !prepared) return;
    canvas.width = prepared.canvas.width;
    canvas.height = prepared.canvas.height;
    canvas.getContext("2d")?.drawImage(prepared.canvas, 0, 0);
  }, [prepared]);

  useEffect(() => {
    const canvas = preview.current;
    if (!canvas || !prepared || !edit) return;
    const frame = requestAnimationFrame(() => {
      const size = fitWithin(coverSize(edit, prepared.canvas.width, prepared.canvas.height), PREVIEW_BOX);
      canvas.width = size.width;
      canvas.height = size.height;
      canvas.getContext("2d")?.putImageData(renderCover(prepared.pixels, edit, size.width, size.height), 0, 0);
    });
    return () => cancelAnimationFrame(frame);
  }, [prepared, edit]);

  const change = (values: Partial<CoverEdit>) => setEdit((current) => current && { ...current, ...values });

  const pointAt = (event: PointerEvent): Point => {
    const box = area.current?.getBoundingClientRect();
    if (!box?.width || !box.height) return { x: 0, y: 0 };
    return { x: (event.clientX - box.left) / box.width, y: (event.clientY - box.top) / box.height };
  };
  const begin = (event: PointerEvent, kind: Drag["kind"], index: number) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { kind, index, last: pointAt(event) };
  };
  const follow = (event: PointerEvent) => {
    const active = drag.current;
    if (!active) return;
    const point = pointAt(event);
    const dx = point.x - active.last.x;
    const dy = point.y - active.last.y;
    active.last = point;
    setEdit((current) => current && {
      ...current,
      corners: active.kind === "corner"
        ? moveCorner(current.corners, active.index, point)
        : active.kind === "side" ? moveSide(current.corners, active.index, dx, dy) : moveAll(current.corners, dx, dy),
    });
  };
  const release = () => {
    drag.current = null;
  };
  const nudge = (index: number) => (event: KeyboardEvent) => {
    const step = event.shiftKey ? 0.02 : 0.004;
    const moves: Record<string, Point> = { ArrowLeft: { x: -step, y: 0 }, ArrowRight: { x: step, y: 0 }, ArrowUp: { x: 0, y: -step }, ArrowDown: { x: 0, y: step } };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    setEdit((current) => current && {
      ...current,
      corners: moveCorner(current.corners, index, { x: current.corners[index].x + move.x, y: current.corners[index].y + move.y }),
    });
  };

  const finish = async () => {
    if (!picture || !edit) return;
    setSaving(true);
    setProblem("");
    // Let the spinner show first: drawing the full-size cover keeps the browser busy for a moment.
    await new Promise((resolve) => setTimeout(resolve, 30));
    try {
      const cover = await coverJpeg(picture, edit);
      const taken = keepPhoto ? await photoJpeg(picture) : null;
      if (!(await onDone(cover, edit, taken))) setSaving(false);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "The cover could not be made");
      setSaving(false);
    }
  };

  const corners = edit?.corners;
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onCancel(); }}>
      <DialogContent className="max-h-[94vh] overflow-y-auto border-white/10 bg-[#0c171a] sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>Edit the cover</DialogTitle>
          <DialogDescription>Drag the round handles onto the corners of the cover, and the bars or the marked area to move it. The marked area is straightened and cropped into the cover shown next to the photo.</DialogDescription>
        </DialogHeader>
        {problem && <p role="alert" className="rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{problem}</p>}
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_17rem]">
          <div className="grid min-h-48 place-items-center rounded-xl border border-white/8 bg-black/30 p-3">
            {!prepared && <Loader2 className="size-5 animate-spin text-muted-foreground" />}
            <div ref={area} className={`relative w-fit max-w-full touch-none select-none ${prepared ? "" : "hidden"}`} onPointerMove={follow} onPointerUp={release} onPointerCancel={release}>
              <canvas ref={stage} className="block h-auto max-h-[62vh] max-w-full" />
              {corners && (
                <>
                  <svg className="absolute inset-0 size-full" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
                    <path d={`M0 0H1V1H0Z M${corners.map((point) => `${point.x} ${point.y}`).join("L")}Z`} fillRule="evenodd" fill="black" fillOpacity={0.55} pointerEvents="none" />
                    <polygon points={corners.map((point) => `${point.x},${point.y}`).join(" ")} fill="transparent" stroke="#40ddc6" strokeWidth={2} vectorEffect="non-scaling-stroke" pointerEvents="all" className="cursor-move" onPointerDown={(event) => begin(event, "area", 0)} />
                  </svg>
                  {corners.map((point, index) => {
                    const next = corners[(index + 1) % 4];
                    return <button key={`side-${index}`} type="button" tabIndex={-1} aria-label={`Move the ${SIDE_NAMES[index]} side`} className={`absolute -translate-x-1/2 -translate-y-1/2 cursor-grab rounded-full border border-white/80 bg-primary/85 ${index % 2 ? "h-8 w-3" : "h-3 w-8"}`} style={{ left: `${((point.x + next.x) / 2) * 100}%`, top: `${((point.y + next.y) / 2) * 100}%` }} onPointerDown={(event) => begin(event, "side", index)} />;
                  })}
                  {corners.map((point, index) => <button key={`corner-${index}`} type="button" aria-label={`The cover's ${CORNER_NAMES[index]} corner. Use the arrow keys to move it.`} className="absolute size-5 -translate-x-1/2 -translate-y-1/2 cursor-grab rounded-full border-2 border-white bg-primary shadow-[0_0_0_3px_rgb(0_0_0/35%)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white" style={{ left: `${point.x * 100}%`, top: `${point.y * 100}%` }} onPointerDown={(event) => begin(event, "corner", index)} onKeyDown={nudge(index)} />)}
                </>
              )}
            </div>
          </div>
          <div className="space-y-5">
            <div className="grid min-h-32 place-items-center rounded-xl border border-white/8 bg-black/30 p-3">
              <canvas ref={preview} role="img" aria-label="The cover as it will be saved" className="block max-w-full rounded-md" />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" disabled={!edit} className="gap-2 border-white/10" onClick={() => setEdit((current) => current && turnLeft(current))}><RotateCcw className="size-3.5" /> Left</Button>
              <Button type="button" variant="outline" size="sm" disabled={!edit} className="gap-2 border-white/10" onClick={() => setEdit((current) => current && turnRight(current))}><RotateCw className="size-3.5" /> Right</Button>
              <Button type="button" variant="outline" size="sm" disabled={!edit} aria-pressed={mirror} className={`gap-2 border-white/10 ${mirror ? "bg-primary/15 text-primary" : ""}`} onClick={() => setEdit((current) => current && mirrored(current))}><FlipHorizontal2 className="size-3.5" /> Mirror</Button>
            </div>
            <Adjustment label="Straighten" value={straighten} min={-45} max={45} step={0.5} unit="°" disabled={!edit} onChange={(value) => change({ straighten: value })} />
            <div className="space-y-2">
              <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Shape</p>
              <div className="flex flex-wrap gap-2">
                {(["square", "marked"] as const).map((shape) => <Button key={shape} type="button" size="sm" variant="outline" disabled={!edit} aria-pressed={edit?.shape === shape} className={`border-white/10 ${edit?.shape === shape ? "bg-primary/15 text-primary" : ""}`} onClick={() => change({ shape })}>{shape === "square" ? "Square, like a CD" : "As marked"}</Button>)}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="ghost" size="sm" disabled={!edit} className="gap-2 text-muted-foreground" onClick={() => change({ corners: wholePicture() })}><Crop className="size-3.5" /> Whole picture</Button>
              <Button type="button" variant="ghost" size="sm" disabled={!picture} className="gap-2 text-muted-foreground" onClick={() => picture && setEdit(defaultEdit(picture.naturalWidth, picture.naturalHeight))}><Undo2 className="size-3.5" /> Start over</Button>
            </div>
            <Adjustment label="Brightness" value={edit?.brightness ?? 0} min={-100} max={100} step={1} disabled={!edit} onChange={(value) => change({ brightness: value })} />
            <Adjustment label="Contrast" value={edit?.contrast ?? 0} min={-100} max={100} step={1} disabled={!edit} onChange={(value) => change({ contrast: value })} />
            <Adjustment label="Saturation" value={edit?.saturation ?? 0} min={-100} max={100} step={1} disabled={!edit} onChange={(value) => change({ saturation: value })} />
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="ghost" disabled={saving} onClick={onCancel}>Cancel</Button>
          <Button type="button" disabled={!edit || !picture || saving} className="gap-2 bg-primary text-primary-foreground" onClick={() => void finish()}>{saving ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />} Use this cover</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Adjustment({ label, value, min, max, step, unit = "", disabled, onChange }: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium uppercase tracking-[0.14em] text-muted-foreground">{label}</span>
        <button type="button" disabled={disabled || value === 0} title={`Set ${label.toLowerCase()} back to 0`} className="font-mono text-muted-foreground enabled:hover:text-foreground" onClick={() => onChange(0)}>{value > 0 ? "+" : ""}{value}{unit}</button>
      </div>
      <Slider aria-label={label} value={[value]} min={min} max={max} step={step} disabled={disabled} onValueChange={([next]) => onChange(next)} />
    </div>
  );
}
