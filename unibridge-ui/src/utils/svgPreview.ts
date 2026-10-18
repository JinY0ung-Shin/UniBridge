/*
 * An SVG is a document and can carry scripts. A blob: URL belongs to the
 * origin that made it, so an SVG previewed through one runs those scripts with
 * the user's UniBridge session once it is opened in a tab of its own ("Open
 * image in new tab"). Decoded in an <img> it never runs them: the SVG is drawn
 * from there onto a canvas, and only the PNG the canvas exports is shown.
 */

const SVG_TYPE = 'image/svg+xml';

/** Longest side, in device pixels, of the PNG an SVG is drawn into. */
const MAX_SIDE = 4096;

/**
 * Size for an SVG without one of its own (some browsers report 0 for it):
 * CSS's default object size, which is what such an SVG gets on a page too.
 */
const DEFAULT_WIDTH = 300;
const DEFAULT_HEIGHT = 150;

export interface SvgRaster {
  png: Blob;
  /** Device pixels per CSS pixel in `png`: its `x` descriptor in a srcset. */
  density: number;
}

/** Whether a file is an SVG, by its name or by its content type. */
export function isSvg(name: string, type: string): boolean {
  return /\.svg$/i.test(name) || type.split(';')[0].trim().toLowerCase() === SVG_TYPE;
}

/**
 * Whether text read from a file named or typed as an SVG is SVG markup at all:
 * a mislabelled PNG or a gzipped .svgz reads as replacement characters.
 */
export function looksLikeSvgMarkup(text: string): boolean {
  const head = text.slice(0, 8192);
  return /<svg[\s>]/i.test(head) && !head.includes('\uFFFD') && !head.includes('\u0000');
}

function readAsDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

/** Decode an SVG in an <img>, which never runs its scripts. */
async function decodeSvg(svg: Blob): Promise<HTMLImageElement> {
  // An <img> decodes an SVG only when it is typed as one. Through a data: URL
  // rather than a blob: one: Chromium taints the canvas for an SVG holding a
  // <foreignObject> (draw.io and Mermaid labels) read from a blob: URL, but not
  // from a data: URL. The URL only ever reaches this off-page <img>.
  const url = await readAsDataUrl(new Blob([svg], { type: SVG_TYPE }));
  const img = new Image();
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error('The SVG could not be decoded'));
    img.src = url;
  });
  return img;
}

/**
 * Draw an SVG into a PNG at the screen's pixel density, its longest side
 * capped at MAX_SIDE. Resolves to null when the SVG cannot be decoded or
 * drawn (no 2D context, a tainted canvas, a failed export); never rejects.
 */
export async function rasterizeSvg(svg: Blob): Promise<SvgRaster | null> {
  try {
    const img = await decodeSvg(svg);
    const width = img.naturalWidth || DEFAULT_WIDTH;
    const height = img.naturalHeight || DEFAULT_HEIGHT;
    const density = Math.min(window.devicePixelRatio || 1, MAX_SIDE / Math.max(width, height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(width * density));
    canvas.height = Math.max(1, Math.round(height * density));
    const context = canvas.getContext('2d');
    if (!context) return null;
    context.drawImage(img, 0, 0, canvas.width, canvas.height);
    // toBlob throws on a tainted canvas and hands back null when it cannot encode.
    const png = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'));
    return png ? { png, density } : null;
  } catch {
    return null;
  }
}
