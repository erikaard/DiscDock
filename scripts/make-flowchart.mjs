// Draws assets/discdock-flow.svg, the flowchart in README.md, in DiscDock's colours with the Lucide icons the
// dashboard uses. Run it from the repository root after changing the flow: node scripts/make-flowchart.mjs
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const WIDTH = 1200;
const HEIGHT = 1760;
const COLORS = { teal: "#40ddc6", sky: "#6cc4f5", amber: "#f2b545", violet: "#b39cff", emerald: "#5ddc9a", rule: "#3a5652" };
const INK = "#0a1416";
const CARD = "#0f1f22";
const EDGE = "#21393d";
const TEXT = "#e9f6f3";
const MUTED = "#8ea8a4";
const FONT = "'Segoe UI', Inter, -apple-system, BlinkMacSystemFont, Roboto, 'Helvetica Neue', Arial, sans-serif";

const ICON_NAMES = [
  "disc-3", "scan-search", "film", "music", "hard-drive", "clapperboard", "list-checks", "disc-album", "life-buoy",
  "audio-lines", "search", "image", "archive", "shield-check", "eject", "cpu", "sparkles", "tags", "library",
  "bell-ring", "layout-dashboard", "hourglass", "disc",
];
const icons = Object.fromEntries(
  await Promise.all(
    ICON_NAMES.map(async (name) => {
      const file = join(root, "node_modules", "lucide-react", "dist", "esm", "icons", `${name}.mjs`);
      return [name, (await import(pathToFileURL(file).href)).__iconNode];
    }),
  ),
);

const escape = (text) => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const colorName = (color) => Object.entries(COLORS).find(([, value]) => value === color)?.[0] ?? "rule";

function icon(name, x, y, size, color) {
  const shapes = icons[name].map(([tag, attributes]) => {
    const list = Object.entries(attributes).filter(([key]) => key !== "key").map(([key, value]) => `${key}="${value}"`);
    return `<${tag} ${list.join(" ")}/>`;
  });
  return `<g transform="translate(${x} ${y}) scale(${size / 24})" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${shapes.join("")}</g>`;
}

// A step: an icon in a tinted square, a title and up to two short lines. Dashed steps only happen sometimes.
function step({ x, y, width, height = 84, iconName, title, lines = [], color, dashed = false }) {
  const iconTop = y + (height - 40) / 2;
  const titleY = lines.length === 2 ? y + 33 : lines.length === 1 ? y + 39 : y + height / 2 + 6;
  const border = dashed ? `stroke="${color}" stroke-opacity="0.7" stroke-dasharray="7 6"` : `stroke="${EDGE}"`;
  return [
    `<rect x="${x}" y="${y}" width="${width}" height="${height}" rx="18" fill="${CARD}" ${border} stroke-width="1.5"/>`,
    `<rect x="${x + 16}" y="${iconTop}" width="40" height="40" rx="12" fill="${color}" fill-opacity="0.15"/>`,
    icon(iconName, x + 24, iconTop + 8, 24, color),
    `<text x="${x + 70}" y="${titleY}" class="title">${escape(title)}</text>`,
    ...lines.map((line, index) => `<text x="${x + 70}" y="${titleY + 20 + index * 17}" class="sub">${escape(line)}</text>`),
  ].join("\n");
}

// A numbered part of a step, such as the passes of the rescue rip.
function part({ x, y, width, height = 64, number, title, line: text, color }) {
  return [
    `<rect x="${x}" y="${y}" width="${width}" height="${height}" rx="14" fill="${CARD}" stroke="${color}" stroke-opacity="0.45" stroke-dasharray="5 5" stroke-width="1.2"/>`,
    `<circle cx="${x + 26}" cy="${y + height / 2}" r="13" fill="${color}" fill-opacity="0.16"/>`,
    `<text x="${x + 26}" y="${y + height / 2 + 5}" text-anchor="middle" class="number" fill="${color}">${number}</text>`,
    `<text x="${x + 50}" y="${y + 28}" class="part">${escape(title)}</text>`,
    `<text x="${x + 50}" y="${y + 46}" class="sub">${escape(text)}</text>`,
  ].join("\n");
}

function pill({ centre, y, width, iconName, label, color }) {
  const x = centre - width / 2;
  return [
    `<rect x="${x}" y="${y}" width="${width}" height="36" rx="18" fill="${color}" fill-opacity="0.12" stroke="${color}" stroke-opacity="0.55"/>`,
    icon(iconName, x + 14, y + 9, 18, color),
    `<text x="${x + 40}" y="${y + 23}" class="pill" fill="${color}">${escape(label)}</text>`,
  ].join("\n");
}

function tag({ x, y, width, iconName, label }) {
  return [
    `<rect x="${x}" y="${y}" width="${width}" height="28" rx="14" fill="${INK}" stroke="#ffffff" stroke-opacity="0.18"/>`,
    icon(iconName, x + 14, y + 6, 16, MUTED),
    `<text x="${x + 38}" y="${y + 19}" class="group">${escape(label)}</text>`,
  ].join("\n");
}

function line(path, color, { dashed = false, arrow = true } = {}) {
  const dash = dashed ? ' stroke-dasharray="7 6"' : "";
  const head = arrow ? ` marker-end="url(#arrow-${colorName(color)})"` : "";
  return `<path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"${dash}${head}/>`;
}

// Down from (x1, y1), across at turnY with rounded corners, and down to (x2, y2).
function elbow(x1, y1, x2, y2, turnY, radius = 14) {
  if (x1 === x2) return `M${x1} ${y1}V${y2}`;
  const direction = x2 > x1 ? 1 : -1;
  const r = Math.min(radius, Math.abs(x2 - x1) / 2);
  return `M${x1} ${y1}V${turnY - r}Q${x1} ${turnY} ${x1 + direction * r} ${turnY}H${x2 - direction * r}Q${x2} ${turnY} ${x2} ${turnY + r}V${y2}`;
}

const favicon = await readFile(join(root, "public", "favicon.svg"), "utf8");
const logo = favicon
  .replace(/<\?xml[^>]*>\s*/, "")
  .replace(/<svg\b[^>]*>/, (svgTag) => `<svg x="40" y="34" width="56" height="56" viewBox="${/viewBox="([^"]+)"/.exec(svgTag)?.[1] ?? "0 0 64 64"}">`);

const parts = [];
parts.push(logo);
parts.push(`<text x="112" y="68" class="heading">How DiscDock works</text>`);
parts.push(`<text x="112" y="96" class="lead">Insert a disc. DiscDock identifies it, rips it, checks it and files it in your library.</text>`);

// Every disc starts the same way.
parts.push(step({ x: 430, y: 140, width: 340, iconName: "disc-3", title: "Insert a disc", lines: ["DiscDock watches every drive"], color: COLORS.teal }));
parts.push(line("M600 224V262", COLORS.teal));
parts.push(step({ x: 430, y: 264, width: 340, iconName: "scan-search", title: "Identify the disc", lines: ["Blu-ray, DVD, audio CD or data disc"], color: COLORS.teal }));
parts.push(line("M600 348V374", COLORS.rule, { arrow: false }));

const lanes = [
  { centre: 300, width: 190, iconName: "film", label: "Movies & series", color: COLORS.teal },
  { centre: 740, width: 132, iconName: "music", label: "Audio CD", color: COLORS.sky },
  { centre: 1045, width: 140, iconName: "hard-drive", label: "Data disc", color: COLORS.violet },
];
for (const lane of lanes) {
  const direction = lane.centre > 600 ? 1 : -1;
  parts.push(line(`M600 374H${lane.centre - direction * 14}Q${lane.centre} 374 ${lane.centre} 388V398`, lane.color));
  parts.push(pill({ ...lane, y: 400 }));
}

// Movies and series: MakeMKV, and a rescue rip for damaged discs.
parts.push(line(elbow(300, 436, 165, 468, 452), COLORS.teal));
parts.push(step({ x: 40, y: 470, width: 250, iconName: "clapperboard", title: "Scan and name", lines: ["MakeMKV reads the titles,", "OMDb names the movie"], color: COLORS.teal }));
parts.push(line("M165 554V588", COLORS.teal));
parts.push(step({ x: 40, y: 590, width: 250, iconName: "list-checks", title: "Choose the titles", lines: ["Main feature, or your pick", "on the dashboard"], color: COLORS.teal }));
parts.push(line("M165 674V708", COLORS.teal));
parts.push(step({ x: 40, y: 710, width: 250, iconName: "disc-album", title: "Rip with MakeMKV", lines: ["The chosen titles become", "MKV files in staging"], color: COLORS.teal }));
parts.push(`<text x="332" y="698" class="note" fill="${COLORS.amber}">IF THE DISC IS DAMAGED</text>`);
parts.push(line("M290 752H328", COLORS.amber, { dashed: true }));
parts.push(step({ x: 330, y: 710, width: 230, iconName: "life-buoy", title: "Rescue rip", lines: ["If MakeMKV hits damage,", "or from Start rescue rip"], color: COLORS.amber, dashed: true }));
parts.push(line("M445 794V828", COLORS.amber, { dashed: true }));
parts.push(part({ x: 330, y: 830, width: 230, number: 1, title: "Copy the movie", line: "skipping unreadable blocks", color: COLORS.amber }));
parts.push(line("M445 894V914", COLORS.amber, { dashed: true }));
parts.push(part({ x: 330, y: 916, width: 230, number: 2, title: "Retry skipped spots", line: "until nothing more comes", color: COLORS.amber }));
parts.push(line("M445 980V1000", COLORS.amber, { dashed: true }));
parts.push(part({ x: 330, y: 1002, width: 230, number: 3, title: "Extract the movie", line: "MakeMKV, or VLC or FFmpeg", color: COLORS.amber }));

// Audio CDs: cyanrip, with the album found on MusicBrainz meanwhile.
parts.push(line("M740 436V468", COLORS.sky));
parts.push(step({ x: 610, y: 470, width: 260, iconName: "audio-lines", title: "Rip to FLAC", lines: ["cyanrip with AccurateRip", "and your drive's offset"], color: COLORS.sky }));
parts.push(line("M740 554V588", COLORS.sky, { dashed: true }));
parts.push(`<text x="752" y="576" class="note" fill="${COLORS.sky}">MEANWHILE</text>`);
parts.push(step({ x: 610, y: 590, width: 260, iconName: "search", title: "Find the album", lines: ["MusicBrainz while it rips,", "or barcode, search, OCR"], color: COLORS.sky }));
parts.push(line("M740 674V708", COLORS.sky));
parts.push(step({ x: 610, y: 710, width: 260, iconName: "image", title: "Pick the cover", lines: ["Cover Art Archive or your", "photo in the cover editor"], color: COLORS.sky }));

// Data discs.
parts.push(line("M1045 436V468", COLORS.violet));
parts.push(step({ x: 930, y: 470, width: 230, iconName: "archive", title: "Save an ISO image", lines: ["An exact copy of", "the whole disc"], color: COLORS.violet }));

// Every rip is checked, and the disc comes out.
const BUS = 1110;
const feeds = [
  [165, 794, COLORS.teal, false],
  [445, 1066, COLORS.amber, true],
  [740, 794, COLORS.sky, false],
  [1045, 554, COLORS.violet, false],
];
for (const [x, top, color, dashed] of feeds) parts.push(line(`M${x} ${top}V${BUS}`, color, { dashed, arrow: false }));
parts.push(line(`M165 ${BUS}H1045`, COLORS.rule, { arrow: false }));
for (const [x, , color] of feeds) parts.push(`<circle cx="${x}" cy="${BUS}" r="4.5" fill="${color}"/>`);
parts.push(line(`M600 ${BUS}V1158`, COLORS.teal));
parts.push(step({ x: 430, y: 1160, width: 340, iconName: "shield-check", title: "Verify the rip", lines: ["FFprobe checks the video and audio", "files before they reach the library"], color: COLORS.emerald }));
parts.push(line("M600 1244V1278", COLORS.teal));
parts.push(step({ x: 430, y: 1280, width: 340, iconName: "eject", title: "Eject the disc", lines: ["The disc comes out as soon", "as the rip is verified"], color: COLORS.teal }));

// What happens after the disc is out. The job keeps its drive until it is done; another drive can rip meanwhile.
parts.push(line("M600 1364V1398", COLORS.teal));
parts.push(`<rect x="40" y="1400" width="1120" height="150" rx="22" fill="${COLORS.teal}" fill-opacity="0.03" stroke="#ffffff" stroke-opacity="0.16" stroke-dasharray="7 6"/>`);
parts.push(tag({ x: 64, y: 1386, width: 214, iconName: "hourglass", label: "After the disc is out" }));
parts.push(tag({ x: 856, y: 1386, width: 280, iconName: "disc", label: "A second drive can rip meanwhile" }));
parts.push(step({ x: 80, y: 1432, width: 320, iconName: "cpu", title: "Transcode with HandBrake", lines: ["Optional: smaller video files"], color: COLORS.teal, dashed: true }));
parts.push(step({ x: 440, y: 1432, width: 320, iconName: "sparkles", title: "Loading screens or AI repair", lines: ["For movies from damaged discs"], color: COLORS.amber, dashed: true }));
parts.push(step({ x: 800, y: 1432, width: 320, iconName: "tags", title: "Name and tag the tracks", lines: ["Or wait in staging for the album"], color: COLORS.sky }));
parts.push(line("M420 1550V1588", COLORS.emerald));
parts.push(step({ x: 260, y: 1590, width: 320, iconName: "library", title: "Move into the library", lines: ["Movies, series, music and ISOs"], color: COLORS.emerald }));
parts.push(line("M580 1632H618", COLORS.emerald));
parts.push(step({ x: 620, y: 1590, width: 320, iconName: "bell-ring", title: "Notify you", lines: ["Discord, Telegram, email and more"], color: COLORS.emerald }));
parts.push(line("M940 1632H1162Q1182 1632 1182 1612V202Q1182 182 1162 182H772", COLORS.teal, { dashed: true }));
parts.push(`<g transform="translate(1182 900) rotate(-90)"><rect x="-52" y="-14" width="104" height="28" rx="14" fill="${INK}" stroke="${COLORS.teal}" stroke-opacity="0.55"/><text x="0" y="5" text-anchor="middle" class="pill" fill="${COLORS.teal}">Next disc</text></g>`);

const footer = "Follow and control every step on the dashboard · several drives at once · jobs survive restarts";
// Roughly how wide the footer is at 14px, to put the icon just before it.
const footerWidth = footer.length * 6.25;
parts.push(icon("layout-dashboard", Math.round(612 - footerWidth / 2 - 28), 1711, 18, COLORS.teal));
parts.push(`<text x="612" y="1726" text-anchor="middle" class="foot">${escape(footer)}</text>`);

const markers = Object.entries(COLORS)
  .map(([name, color]) => `<marker id="arrow-${name}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11" orient="auto-start-reverse"><path d="M1 1.5L9 5L1 8.5Z" fill="${color}"/></marker>`)
  .join("\n");

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}" role="img" aria-labelledby="flow-title flow-description">
<title id="flow-title">How DiscDock works</title>
<desc id="flow-description">A disc is inserted and identified. Movies and series are scanned, named and ripped with MakeMKV. When MakeMKV hits damage, a rescue rip copies the movie while skipping unreadable blocks, retries the skipped spots, and extracts the movie with MakeMKV, VLC or FFmpeg. Audio CDs are ripped to FLAC with cyanrip while the album is found on MusicBrainz. Data discs are saved as ISO images. Every rip is verified and the disc is ejected. Then movies can be transcoded or get loading screens or AI repair, and CD tracks are named and tagged or wait in staging for their album, before everything moves into the library and you are notified. A second drive can rip meanwhile.</desc>
<!-- Generated by scripts/make-flowchart.mjs. Icons: Lucide (ISC license). -->
<defs>
<radialGradient id="glow" cx="88%" cy="0%" r="75%"><stop offset="0" stop-color="${COLORS.teal}" stop-opacity="0.16"/><stop offset="1" stop-color="${COLORS.teal}" stop-opacity="0"/></radialGradient>
${markers}
</defs>
<style>
text { font-family: ${FONT}; }
.heading { font-size: 30px; font-weight: 700; fill: ${TEXT}; letter-spacing: -0.01em; }
.lead { font-size: 16px; fill: ${MUTED}; }
.title { font-size: 16px; font-weight: 600; fill: ${TEXT}; }
.part { font-size: 14px; font-weight: 600; fill: ${TEXT}; }
.number { font-size: 13px; font-weight: 700; }
.sub { font-size: 12.5px; fill: ${MUTED}; }
.pill { font-size: 14px; font-weight: 600; }
.note { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; }
.group { font-size: 13px; font-weight: 600; fill: ${MUTED}; }
.foot { font-size: 14px; fill: ${MUTED}; }
</style>
<rect width="${WIDTH}" height="${HEIGHT}" rx="28" fill="${INK}"/>
<rect width="${WIDTH}" height="${HEIGHT}" rx="28" fill="url(#glow)"/>
<rect x="0.75" y="0.75" width="${WIDTH - 1.5}" height="${HEIGHT - 1.5}" rx="27.5" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>
${parts.join("\n")}
</svg>
`;

await writeFile(join(root, "assets", "discdock-flow.svg"), svg, "utf8");
console.log(`Wrote assets/discdock-flow.svg (${Math.round(svg.length / 1024)} KB)`);
