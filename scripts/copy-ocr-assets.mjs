// Copies Tesseract's worker, engine and English and Norwegian language data into public/ocr, so the
// dashboard can read CD cases without downloading anything while it runs. Runs before every dashboard build.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const modules = join(root, "node_modules");
const target = join(root, "public", "ocr");
const files = [
  ["tesseract.js/dist/worker.min.js", "worker.min.js"],
  // tesseract.js picks one of these engines by what the browser supports (LSTM recognition only).
  ["tesseract.js-core/tesseract-core-relaxedsimd-lstm.wasm.js", "core/tesseract-core-relaxedsimd-lstm.wasm.js"],
  ["tesseract.js-core/tesseract-core-simd-lstm.wasm.js", "core/tesseract-core-simd-lstm.wasm.js"],
  ["tesseract.js-core/tesseract-core-lstm.wasm.js", "core/tesseract-core-lstm.wasm.js"],
  ["@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz", "lang/eng.traineddata.gz"],
  ["@tesseract.js-data/nor/4.0.0_best_int/nor.traineddata.gz", "lang/nor.traineddata.gz"],
];
for (const [source, destination] of files) {
  const path = join(target, destination);
  mkdirSync(dirname(path), { recursive: true });
  copyFileSync(join(modules, source), path);
}
console.log(`Copied ${files.length} OCR files to public/ocr`);
