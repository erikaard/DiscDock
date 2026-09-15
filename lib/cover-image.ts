// A cover from a photo of a CD case, made in the browser: the photo is turned upright, the cover's four
// corners are marked on it, and that area is straightened into a picture with its colours adjusted.

export type Point = { x: number; y: number };
export type Corners = [Point, Point, Point, Point];

export type CoverEdit = {
  // Quarter turns clockwise, 0 to 3.
  turns: number;
  // A fine turn in degrees, from -45 to 45, for a photo taken at a slight tilt.
  straighten: number;
  mirror: boolean;
  // The cover's top left, top right, bottom right and bottom left corners on the turned photo, from 0 to 1.
  corners: Corners;
  // "square" always makes a CD cover; "marked" keeps the proportions of the marked area.
  shape: "square" | "marked";
  // From -100 to 100; 0 leaves the photo as it is.
  brightness: number;
  contrast: number;
  saturation: number;
};

type Turn = Pick<CoverEdit, "turns" | "straighten" | "mirror">;
type Picture = HTMLImageElement | HTMLCanvasElement;
type Size = { width: number; height: number };
type Homography = [number, number, number, number, number, number, number, number];

// Covers are saved at most this large, and photos are read at most EDIT_SOURCE_MAX pixels wide or high.
const COVER_MAX = 1400;
const COVER_MIN = 500;
const EDIT_SOURCE_MAX = 2400;

const clamp = (value: number, low = 0, high = 1) => Math.min(high, Math.max(low, value));
const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

export function wholePicture(): Corners {
  return [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }];
}

// A centred square over most of the picture, where the cover usually is in a photo of the case.
function centredSquare(width: number, height: number): Corners {
  const side = Math.min(width, height) * 0.8;
  const left = (width - side) / 2 / width;
  const top = (height - side) / 2 / height;
  return [{ x: left, y: top }, { x: 1 - left, y: top }, { x: 1 - left, y: 1 - top }, { x: left, y: 1 - top }];
}

export function defaultEdit(width: number, height: number): CoverEdit {
  return { turns: 0, straighten: 0, mirror: false, corners: centredSquare(width, height), shape: "square", brightness: 0, contrast: 0, saturation: 0 };
}

// A saved edit, or null when it is not one.
export function parseCoverEdit(value: unknown): CoverEdit | null {
  if (!value || typeof value !== "object") return null;
  const edit = value as Record<string, unknown>;
  const numbers = ["turns", "straighten", "brightness", "contrast", "saturation"].every((name) => Number.isFinite(edit[name]));
  const corners = Array.isArray(edit.corners) && edit.corners.length === 4
    && edit.corners.every((point: unknown) => Boolean(point) && Number.isFinite((point as Point).x) && Number.isFinite((point as Point).y));
  if (!numbers || !corners) return null;
  return {
    turns: ((Math.round(edit.turns as number) % 4) + 4) % 4,
    straighten: clamp(edit.straighten as number, -45, 45),
    mirror: edit.mirror === true,
    corners: (edit.corners as Point[]).map((point) => ({ x: clamp(point.x), y: clamp(point.y) })) as Corners,
    shape: edit.shape === "marked" ? "marked" : "square",
    brightness: clamp(edit.brightness as number, -100, 100),
    contrast: clamp(edit.contrast as number, -100, 100),
    saturation: clamp(edit.saturation as number, -100, 100),
  };
}

export function moveCorner(corners: Corners, index: number, to: Point): Corners {
  return corners.map((point, position) => (position === index ? { x: clamp(to.x), y: clamp(to.y) } : point)) as Corners;
}

// Moves the corners at both ends of a side: 0 is the top, 1 the right, 2 the bottom and 3 the left side.
export function moveSide(corners: Corners, side: number, dx: number, dy: number): Corners {
  return shift(corners, [side, (side + 1) % 4], dx, dy);
}

export function moveAll(corners: Corners, dx: number, dy: number): Corners {
  return shift(corners, [0, 1, 2, 3], dx, dy);
}

// Moves some corners together, only as far as all of them stay on the picture.
function shift(corners: Corners, which: number[], dx: number, dy: number): Corners {
  const moving = which.map((index) => corners[index]);
  const x = clamp(dx, -Math.min(...moving.map((point) => point.x)), 1 - Math.max(...moving.map((point) => point.x)));
  const y = clamp(dy, -Math.min(...moving.map((point) => point.y)), 1 - Math.max(...moving.map((point) => point.y)));
  return corners.map((point, index) => (which.includes(index) ? { x: point.x + x, y: point.y + y } : point)) as Corners;
}

// A quarter turn clockwise; the marked corners turn with the picture.
export function turnRight(edit: CoverEdit): CoverEdit {
  const [a, b, c, d] = edit.corners.map(({ x, y }) => ({ x: 1 - y, y: x }));
  return { ...edit, turns: (edit.turns + 1) % 4, corners: [d, a, b, c] };
}

export function turnLeft(edit: CoverEdit): CoverEdit {
  const [a, b, c, d] = edit.corners.map(({ x, y }) => ({ x: y, y: 1 - x }));
  return { ...edit, turns: (edit.turns + 3) % 4, corners: [b, c, d, a] };
}

// Mirrors the picture as it is shown. The photo is mirrored before it is turned, so the turn is reversed.
export function mirrored(edit: CoverEdit): CoverEdit {
  const [a, b, c, d] = edit.corners.map(({ x, y }) => ({ x: 1 - x, y }));
  return { ...edit, mirror: !edit.mirror, turns: (4 - edit.turns) % 4, straighten: -edit.straighten, corners: [b, a, d, c] };
}

function pictureSize(picture: Picture): Size {
  return picture instanceof HTMLImageElement
    ? { width: picture.naturalWidth, height: picture.naturalHeight }
    : { width: picture.width, height: picture.height };
}

function drawingContext(canvas: HTMLCanvasElement): CanvasRenderingContext2D {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("This browser cannot edit pictures");
  return context;
}

// The photo mirrored, turned and straightened, at most maxSide pixels wide or high.
export function preparePhoto(picture: Picture, turn: Turn, maxSide: number): HTMLCanvasElement {
  const natural = pictureSize(picture);
  const scale = Math.min(1, maxSide / Math.max(natural.width, natural.height));
  const width = natural.width * scale;
  const height = natural.height * scale;
  const angle = ((turn.turns * 90 + turn.straighten) * Math.PI) / 180;
  const cos = Math.abs(Math.cos(angle));
  const sin = Math.abs(Math.sin(angle));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * cos + height * sin));
  canvas.height = Math.max(1, Math.round(width * sin + height * cos));
  const context = drawingContext(canvas);
  context.imageSmoothingQuality = "high";
  context.translate(canvas.width / 2, canvas.height / 2);
  context.rotate(angle);
  if (turn.mirror) context.scale(-1, 1);
  context.drawImage(picture, -width / 2, -height / 2, width, height);
  return canvas;
}

export function readPixels(canvas: HTMLCanvasElement): ImageData {
  return drawingContext(canvas).getImageData(0, 0, canvas.width, canvas.height);
}

// The size of the finished cover: square, or with the proportions of the marked area, 500 to 1400 pixels.
export function coverSize(edit: CoverEdit, width: number, height: number): Size {
  const [a, b, c, d] = edit.corners.map((point) => ({ x: point.x * width, y: point.y * height }));
  const across = (distance(a, b) + distance(d, c)) / 2;
  const down = (distance(a, d) + distance(b, c)) / 2;
  if (edit.shape === "square") {
    const side = Math.round(clamp((across + down) / 2, COVER_MIN, COVER_MAX));
    return { width: side, height: side };
  }
  const longest = Math.max(across, down, 1);
  const scale = clamp(1, COVER_MIN / longest, COVER_MAX / longest);
  return { width: Math.max(1, Math.round(across * scale)), height: Math.max(1, Math.round(down * scale)) };
}

export function fitWithin(size: Size, box: number): Size {
  const scale = Math.min(box / size.width, box / size.height);
  return { width: Math.max(1, Math.round(size.width * scale)), height: Math.max(1, Math.round(size.height * scale)) };
}

// The projective map from the unit square onto the corners p0 (0, 0), p1 (1, 0), p2 (1, 1) and p3 (0, 1).
export function squareToQuad(p0: Point, p1: Point, p2: Point, p3: Point): Homography {
  const dx1 = p1.x - p2.x;
  const dx2 = p3.x - p2.x;
  const dx3 = p0.x - p1.x + p2.x - p3.x;
  const dy1 = p1.y - p2.y;
  const dy2 = p3.y - p2.y;
  const dy3 = p0.y - p1.y + p2.y - p3.y;
  const denominator = dx1 * dy2 - dx2 * dy1;
  if (Math.abs(denominator) < 1e-12 || (Math.abs(dx3) < 1e-12 && Math.abs(dy3) < 1e-12)) {
    return [p1.x - p0.x, p3.x - p0.x, p0.x, p1.y - p0.y, p3.y - p0.y, p0.y, 0, 0];
  }
  const g = (dx3 * dy2 - dx2 * dy3) / denominator;
  const h = (dx1 * dy3 - dx3 * dy1) / denominator;
  return [p1.x - p0.x + g * p1.x, p3.x - p0.x + h * p3.x, p0.x, p1.y - p0.y + g * p1.y, p3.y - p0.y + h * p3.y, p0.y, g, h];
}

export function mapPoint(map: Homography, u: number, v: number): Point {
  const w = map[6] * u + map[7] * v + 1;
  return { x: (map[0] * u + map[1] * v + map[2]) / w, y: (map[3] * u + map[4] * v + map[5]) / w };
}

// The marked area of a prepared photo, straightened into a picture of the given size, colours adjusted.
export function renderCover(pixels: ImageData, edit: CoverEdit, width: number, height: number): ImageData {
  const source = pixels.data;
  const sourceWidth = pixels.width;
  const sourceHeight = pixels.height;
  const [p0, p1, p2, p3] = edit.corners.map((point) => ({ x: point.x * sourceWidth, y: point.y * sourceHeight }));
  const map = squareToQuad(p0, p1, p2, p3);
  const output = new ImageData(width, height);
  const target = output.data;
  const adjust = edit.brightness !== 0 || edit.contrast !== 0 || edit.saturation !== 0;
  const contrast = (100 + edit.contrast) / 100;
  const brightness = edit.brightness * 1.28;
  const saturation = (100 + edit.saturation) / 100;
  for (let row = 0; row < height; row += 1) {
    const v = (row + 0.5) / height;
    for (let column = 0; column < width; column += 1) {
      const u = (column + 0.5) / width;
      const w = map[6] * u + map[7] * v + 1;
      const x = clamp((map[0] * u + map[1] * v + map[2]) / w - 0.5, 0, sourceWidth - 1);
      const y = clamp((map[3] * u + map[4] * v + map[5]) / w - 0.5, 0, sourceHeight - 1);
      const x0 = Math.floor(x);
      const y0 = Math.floor(y);
      const x1 = Math.min(sourceWidth - 1, x0 + 1);
      const y1 = Math.min(sourceHeight - 1, y0 + 1);
      const fx = x - x0;
      const fy = y - y0;
      const w00 = (1 - fx) * (1 - fy);
      const w10 = fx * (1 - fy);
      const w01 = (1 - fx) * fy;
      const w11 = fx * fy;
      const i00 = (y0 * sourceWidth + x0) * 4;
      const i10 = (y0 * sourceWidth + x1) * 4;
      const i01 = (y1 * sourceWidth + x0) * 4;
      const i11 = (y1 * sourceWidth + x1) * 4;
      let red = source[i00] * w00 + source[i10] * w10 + source[i01] * w01 + source[i11] * w11;
      let green = source[i00 + 1] * w00 + source[i10 + 1] * w10 + source[i01 + 1] * w01 + source[i11 + 1] * w11;
      let blue = source[i00 + 2] * w00 + source[i10 + 2] * w10 + source[i01 + 2] * w01 + source[i11 + 2] * w11;
      if (adjust) {
        red = (red - 128) * contrast + 128 + brightness;
        green = (green - 128) * contrast + 128 + brightness;
        blue = (blue - 128) * contrast + 128 + brightness;
        const grey = 0.299 * red + 0.587 * green + 0.114 * blue;
        red = grey + (red - grey) * saturation;
        green = grey + (green - grey) * saturation;
        blue = grey + (blue - grey) * saturation;
      }
      const index = (row * width + column) * 4;
      // The clamped array rounds and limits each value to 0-255.
      target[index] = red;
      target[index + 1] = green;
      target[index + 2] = blue;
      target[index + 3] = 255;
    }
  }
  return output;
}

function jpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("The picture could not be saved"))), "image/jpeg", quality);
  });
}

// The finished cover as a JPEG. It is drawn at twice its size and scaled down, so fine detail stays smooth.
export function coverJpeg(picture: Picture, edit: CoverEdit): Promise<Blob> {
  const prepared = preparePhoto(picture, edit, EDIT_SOURCE_MAX);
  const size = coverSize(edit, prepared.width, prepared.height);
  const large = document.createElement("canvas");
  large.width = size.width * 2;
  large.height = size.height * 2;
  drawingContext(large).putImageData(renderCover(readPixels(prepared), edit, large.width, large.height), 0, 0);
  const cover = document.createElement("canvas");
  cover.width = size.width;
  cover.height = size.height;
  const context = drawingContext(cover);
  context.imageSmoothingQuality = "high";
  context.drawImage(large, 0, 0, size.width, size.height);
  return jpeg(cover, 0.92);
}

// The photo as taken, upright and at most 2400 pixels, kept so the cover can be edited again.
export function photoJpeg(picture: Picture): Promise<Blob> {
  return jpeg(preparePhoto(picture, { turns: 0, straighten: 0, mirror: false }, EDIT_SOURCE_MAX), 0.9);
}

// The browser turns the picture upright from its EXIF orientation while it decodes it.
export function loadPicture(photo: Blob): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(photo);
    const picture = new Image();
    picture.onload = () => {
      URL.revokeObjectURL(url);
      resolve(picture);
    };
    picture.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("The photo could not be opened. Choose a JPEG or PNG picture."));
    };
    picture.src = url;
  });
}
