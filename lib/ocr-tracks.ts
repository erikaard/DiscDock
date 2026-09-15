// Track names read from a photo of the back of a CD case, from the text Tesseract recognised.

// "4:23", "(4.23)" or "[04:23]" at the end of a line.
const DURATION = /\s*[([]?\d{1,2}[:.]\d{2}[)\]]?\s*$/;
// "1 Brevet", "01. Brevet", "2) Danse, danse", "3 - Så nær, så nær".
const NUMBERED = /^\s*(\d{1,2})(?:\s*[.):\-–—]\s*|\s+)(.+)$/;
// Lines on a case that are not track names: durations, catalogue numbers, copyright lines, web addresses.
const NOT_A_TITLE = /^[\d\s:.()[\]\-/]+$|©|℗|\((?:p|c)\)|www\.|https?:|all rights reserved|made in /i;

function cleanTitle(text: string): string {
  return text
    .replace(DURATION, "")
    .replace(/\s*\.{2,}\s*$/, "")
    .replace(/^[\s"'“”‘’*•·|_-]+|[\s"'“”‘’*•·|_-]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// One name per track of the CD, "" where none was read. Numbered lines are placed by their number;
// without numbers the lines are taken in order.
export function tracksFromText(text: string, trackCount: number): string[] {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const byNumber = new Map<number, string>();
  for (const line of lines) {
    const match = NUMBERED.exec(line);
    if (!match) continue;
    const number = Number(match[1]);
    const title = cleanTitle(match[2]);
    if (number >= 1 && (!trackCount || number <= trackCount) && title && !NOT_A_TITLE.test(title) && !byNumber.has(number)) {
      byNumber.set(number, title);
    }
  }
  if (byNumber.size >= 2) {
    const count = trackCount || Math.max(...byNumber.keys());
    return Array.from({ length: count }, (_, index) => byNumber.get(index + 1) ?? "");
  }
  const titles = lines.map(cleanTitle).filter((title) => title && !NOT_A_TITLE.test(title));
  return Array.from({ length: trackCount || titles.length }, (_, index) => titles[index] ?? "");
}
