// The camera is used for the barcode and for photos of a CD case. Browsers allow it on 127.0.0.1.

export function cameraAvailable(): boolean {
  return typeof navigator !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);
}

export function cameraProblem(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "The browser was not allowed to use the camera. Allow the camera for this page next to the address bar, then try again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No camera was found. Connect one, or type the barcode or choose a photo instead.";
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return "The camera is in use by another program. Close that program, then try again.";
  }
  return error instanceof Error && error.message ? error.message : "The camera could not be started.";
}

// The camera on the back of a phone or tablet when there is one, at a resolution a barcode can be read at.
export const CASE_CAMERA: MediaStreamConstraints = {
  audio: false,
  video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
};
