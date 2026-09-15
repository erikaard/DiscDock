"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { BarcodeFormat, BrowserMultiFormatReader, type IScannerControls } from "@zxing/browser";
import { DecodeHintType } from "@zxing/library";
import { CASE_CAMERA, cameraProblem } from "@/lib/camera";

// Reads the barcode on the back of a CD case from the camera and reports it as soon as it is recognised.
export function BarcodeScanner({ onBarcode }: { onBarcode: (barcode: string) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const report = useRef(onBarcode);
  const [starting, setStarting] = useState(true);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    report.current = onBarcode;
  }, [onBarcode]);

  useEffect(() => {
    const preview = video.current;
    if (!preview) return;
    // CDs carry EAN-13 or UPC-A codes; the short forms are accepted too.
    const hints = new Map([[DecodeHintType.POSSIBLE_FORMATS, [BarcodeFormat.EAN_13, BarcodeFormat.UPC_A, BarcodeFormat.EAN_8, BarcodeFormat.UPC_E]]]);
    const reader = new BrowserMultiFormatReader(hints, { delayBetweenScanAttempts: 120 });
    let controls: IScannerControls | undefined;
    let finished = false;
    reader
      .decodeFromConstraints(CASE_CAMERA, preview, (result, _error, scanning) => {
        if (!result || finished) return;
        finished = true;
        scanning?.stop();
        report.current(result.getText());
      })
      .then((started) => {
        controls = started;
        setStarting(false);
        if (finished) started.stop();
      })
      .catch((error: unknown) => {
        setStarting(false);
        setProblem(cameraProblem(error));
      });
    return () => {
      finished = true;
      controls?.stop();
    };
  }, []);

  return (
    <div className="mt-3 space-y-2">
      <div className="relative overflow-hidden rounded-xl border border-white/10 bg-black">
        <video ref={video} muted playsInline className="aspect-video w-full object-cover" />
        <div className="pointer-events-none absolute inset-x-[12%] top-1/2 h-1/3 -translate-y-1/2 rounded-lg border-2 border-primary/70" />
        {starting && !problem && <div className="absolute inset-0 grid place-items-center text-white/80"><Loader2 className="size-5 animate-spin" /></div>}
      </div>
      {problem ? (
        <p role="alert" className="rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{problem}</p>
      ) : (
        <p className="text-xs leading-5 text-muted-foreground">Hold the barcode on the back of the case inside the frame. DiscDock searches MusicBrainz as soon as it can read it.</p>
      )}
    </div>
  );
}
