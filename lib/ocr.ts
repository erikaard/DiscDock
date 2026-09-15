// Reads the text on a photo of a CD case with Tesseract, in the browser. DiscDock serves the worker, the
// engine and the English and Norwegian language data itself (public/ocr), so nothing is downloaded while it runs.

export async function readCaseText(photo: Blob, onProgress?: (share: number) => void): Promise<string> {
  // Loaded only when a photo is read, so the dashboard itself stays small.
  const { createWorker } = await import("tesseract.js");
  const base = new URL("/ocr/", window.location.origin).href;
  const worker = await createWorker(["eng", "nor"], 1 /* OEM.LSTM_ONLY */, {
    workerPath: `${base}worker.min.js`,
    corePath: `${base}core`,
    langPath: `${base}lang`,
    gzip: true,
    logger: (message) => {
      if (message.status === "recognizing text") onProgress?.(message.progress);
    },
  });
  try {
    const { data } = await worker.recognize(photo);
    return data.text;
  } finally {
    await worker.terminate();
  }
}
