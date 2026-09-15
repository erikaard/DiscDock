"use client";

import { useEffect, useRef, useState } from "react";
import { Camera, ImagePlus, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CASE_CAMERA, cameraAvailable, cameraProblem } from "@/lib/camera";

// A photo of a CD case from the camera, or from a picture file, for example one taken with a phone.
export function PhotoCapture({ label, disabled = false, onPhoto }: { label: string; disabled?: boolean; onPhoto: (photo: Blob) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [opening, setOpening] = useState(false);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    if (!stream) return;
    const preview = video.current;
    if (preview) {
      preview.srcObject = stream;
      void preview.play().catch(() => undefined);
    }
    return () => stream.getTracks().forEach((track) => track.stop());
  }, [stream]);

  const open = async () => {
    setProblem("");
    setOpening(true);
    try {
      setStream(await navigator.mediaDevices.getUserMedia(CASE_CAMERA));
    } catch (error) {
      setProblem(cameraProblem(error));
    } finally {
      setOpening(false);
    }
  };

  const take = () => {
    const preview = video.current;
    if (!preview?.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = preview.videoWidth;
    canvas.height = preview.videoHeight;
    canvas.getContext("2d")?.drawImage(preview, 0, 0);
    canvas.toBlob((photo) => {
      if (!photo) return;
      setStream(null);
      onPhoto(photo);
    }, "image/jpeg", 0.92);
  };

  return (
    <div className="space-y-2">
      {stream ? (
        <>
          <video ref={video} muted playsInline className="aspect-video w-full rounded-xl border border-white/10 bg-black object-cover" />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" className="gap-2 bg-primary text-primary-foreground" onClick={take}><Camera className="size-3.5" /> Take the photo</Button>
            <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setStream(null)}>Close the camera</Button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap gap-2">
          {cameraAvailable() && <Button disabled={disabled || opening} variant="outline" size="sm" className="gap-2 border-white/10" onClick={() => void open()}>{opening ? <Loader2 className="size-3.5 animate-spin" /> : <Camera className="size-3.5" />} {label}</Button>}
          <Button disabled={disabled} variant="outline" size="sm" className="gap-2 border-white/10" onClick={() => file.current?.click()}><ImagePlus className="size-3.5" /> Choose a photo</Button>
          <input ref={file} type="file" accept="image/jpeg,image/png" hidden onChange={(event) => { const chosen = event.target.files?.[0]; if (chosen) onPhoto(chosen); event.target.value = ""; }} />
        </div>
      )}
      {problem && <p role="alert" className="rounded-lg border border-amber-400/20 bg-amber-400/8 p-3 text-xs leading-5 text-amber-100">{problem}</p>}
    </div>
  );
}
