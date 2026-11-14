export interface FormatBytesOptions {
  /** IEC labels (KiB, MiB, …) instead of KB, MB, …; both count in steps of 1024. */
  iec?: boolean;
  /** Keep one decimal ("2.0 KB"); by default a trailing ".0" is dropped ("2 KB"). */
  fixedDecimal?: boolean;
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
const IEC_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

/** A byte count for display ("512 B", "1.5 MB"); null (size unknown) is "—". */
export function formatBytes(bytes: number | null, { iec = false, fixedDecimal = false }: FormatBytesOptions = {}): string {
  if (bytes === null) return '—';
  const units = iec ? IEC_UNITS : UNITS;
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  if (unit === 0) return `${Math.round(size)} B`;
  const text = size.toFixed(1);
  return `${fixedDecimal ? text : parseFloat(text)} ${units[unit]}`;
}
