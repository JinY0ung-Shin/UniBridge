import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { isSvg, looksLikeSvgMarkup, rasterizeSvg } from '../utils/svgPreview';

const originalCreateObjectUrl = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectUrl = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
const originalDevicePixelRatio = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio');

function restoreProperty(target: object, key: PropertyKey, descriptor: PropertyDescriptor | undefined) {
  if (descriptor) {
    Object.defineProperty(target, key, descriptor);
  } else {
    Reflect.deleteProperty(target, key);
  }
}

function setDevicePixelRatio(ratio: number) {
  Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: ratio });
}

interface DrawingOptions {
  naturalWidth?: number;
  naturalHeight?: number;
  decodes?: boolean;
  context?: boolean;
  exported?: 'png' | 'nothing' | 'tainted';
}

/** jsdom neither decodes images nor draws on a canvas: stand in for both. */
function stubDrawing({
  naturalWidth = 64,
  naturalHeight = 32,
  decodes = true,
  context = true,
  exported = 'png',
}: DrawingOptions = {}) {
  const imageSources: string[] = [];
  const setSrc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src')!.set!;
  vi.spyOn(HTMLImageElement.prototype, 'src', 'set').mockImplementation(function (
    this: HTMLImageElement,
    value: string,
  ) {
    setSrc.call(this, value);
    imageSources.push(value);
    setTimeout(() => this.dispatchEvent(new Event(decodes ? 'load' : 'error')), 0);
  });
  vi.spyOn(HTMLImageElement.prototype, 'naturalWidth', 'get').mockReturnValue(naturalWidth);
  vi.spyOn(HTMLImageElement.prototype, 'naturalHeight', 'get').mockReturnValue(naturalHeight);
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context ? ({ drawImage } as unknown as CanvasRenderingContext2D) : null,
  );
  const canvasSizes: number[][] = [];
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
    this: HTMLCanvasElement,
    callback: BlobCallback,
    type?: string,
  ) {
    canvasSizes.push([this.width, this.height]);
    if (exported === 'tainted') throw new DOMException('The canvas has been tainted', 'SecurityError');
    callback(exported === 'png' ? new Blob(['png'], { type }) : null);
  });
  return { imageSources, drawImage, canvasSizes };
}

describe('isSvg', () => {
  it('knows an SVG by its extension, in any case', () => {
    expect(isSvg('logo.svg', '')).toBe(true);
    expect(isSvg('LOGO.SVG', 'application/octet-stream')).toBe(true);
  });

  it('knows an SVG by its content type, whatever the name', () => {
    expect(isSvg('badge.png', 'image/svg+xml')).toBe(true);
    expect(isSvg('badge', 'Image/SVG+XML; charset=utf-8')).toBe(true);
  });

  it('leaves other files alone', () => {
    expect(isSvg('photo.png', 'image/png')).toBe(false);
    expect(isSvg('logo.svg.png', 'image/png')).toBe(false);
    expect(isSvg('logo.svgz', 'application/gzip')).toBe(false);
  });
});

describe('rasterizeSvg', () => {
  const svgText = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"><script>alert(1)</script></svg>';
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    createObjectURL = vi.fn(() => 'blob:svg');
    revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    setDevicePixelRatio(1);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    restoreProperty(URL, 'createObjectURL', originalCreateObjectUrl);
    restoreProperty(URL, 'revokeObjectURL', originalRevokeObjectUrl);
    restoreProperty(window, 'devicePixelRatio', originalDevicePixelRatio);
  });

  it('draws the SVG into a PNG at the screen pixel density', async () => {
    setDevicePixelRatio(2);
    const { drawImage, canvasSizes } = stubDrawing({ naturalWidth: 64, naturalHeight: 32 });

    const raster = await rasterizeSvg(new Blob([svgText], { type: 'image/svg+xml' }));

    expect(raster?.png.type).toBe('image/png');
    expect(raster?.density).toBe(2);
    expect(canvasSizes).toEqual([[128, 64]]);
    expect(drawImage).toHaveBeenCalledWith(expect.any(HTMLImageElement), 0, 0, 128, 64);
  });

  it('decodes the SVG in an off-page <img> through a data: URL, never an object URL', async () => {
    const { imageSources } = stubDrawing();

    // Typed as an SVG for the <img> even when the download did not say so.
    await rasterizeSvg(new Blob([svgText]));

    // A blob: URL would make Chromium taint the canvas for a <foreignObject>.
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(imageSources).toHaveLength(1);
    const [prefix, payload] = imageSources[0].split(',');
    expect(prefix).toBe('data:image/svg+xml;base64');
    expect(atob(payload)).toBe(svgText);
  });

  it('caps the longest side at 4096 pixels and keeps the aspect ratio', async () => {
    setDevicePixelRatio(2);
    const { canvasSizes } = stubDrawing({ naturalWidth: 10000, naturalHeight: 2500 });

    const raster = await rasterizeSvg(new Blob([svgText], { type: 'image/svg+xml' }));

    expect(canvasSizes).toEqual([[4096, 1024]]);
    expect(raster?.density).toBeCloseTo(0.4096);
  });

  it('gives an SVG without a size of its own the CSS default object size', async () => {
    const { canvasSizes } = stubDrawing({ naturalWidth: 0, naturalHeight: 0 });

    const raster = await rasterizeSvg(new Blob([svgText], { type: 'image/svg+xml' }));

    expect(canvasSizes).toEqual([[300, 150]]);
    expect(raster?.density).toBe(1);
  });

  it.each<[string, DrawingOptions]>([
    ['does not decode', { decodes: false }],
    ['gets no 2D context', { context: false }],
    ['taints the canvas', { exported: 'tainted' }],
    ['cannot be exported', { exported: 'nothing' }],
  ])('resolves to null for an SVG that %s, without making an object URL', async (_label, options) => {
    stubDrawing(options);

    await expect(rasterizeSvg(new Blob([svgText], { type: 'image/svg+xml' }))).resolves.toBeNull();
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });
});

describe('looksLikeSvgMarkup', () => {
  it('accepts SVG markup, with or without a prolog', () => {
    expect(looksLikeSvgMarkup('<svg xmlns="http://www.w3.org/2000/svg"/>')).toBe(true);
    expect(looksLikeSvgMarkup('<?xml version="1.0"?>\n<!-- logo -->\n<svg>\n</svg>')).toBe(true);
  });

  it('rejects text that is not SVG markup', () => {
    expect(looksLikeSvgMarkup('\uFFFDPNG\r\n\u001a\n\uFFFD<svg>')).toBe(false);
    expect(looksLikeSvgMarkup('\u001f\u008b\u0008\u0000<svg>')).toBe(false);
    expect(looksLikeSvgMarkup('just text')).toBe(false);
    expect(looksLikeSvgMarkup('<svgfoo>')).toBe(false);
  });
});
