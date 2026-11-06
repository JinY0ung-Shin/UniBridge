/**
 * CSV file bytes → text. Excel on Korean Windows saves "CSV" as CP949 (a
 * superset of EUC-KR that the WHATWG "euc-kr" decoder covers), newer Excel
 * and most tools save UTF-8 (often with a BOM), and "Unicode text" is UTF-16LE
 * with a BOM. Strict UTF-8 is tried first: a CP949 file is never valid UTF-8
 * once it holds Korean, so the fallback only fires when it has to.
 */
export type CsvEncoding = 'utf-8' | 'utf-16le' | 'utf-16be' | 'euc-kr';

export interface DecodedCsv {
  text: string;
  encoding: CsvEncoding;
}

export function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

export function decodeCsvBytes(input: ArrayBuffer | Uint8Array): DecodedCsv {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return { text: stripBom(new TextDecoder('utf-16le').decode(bytes)), encoding: 'utf-16le' };
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return { text: stripBom(new TextDecoder('utf-16be').decode(bytes)), encoding: 'utf-16be' };
  }
  try {
    // ignoreBOM: keep the BOM in the output so stripBom handles every path alike.
    const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    return { text: stripBom(text), encoding: 'utf-8' };
  } catch {
    return { text: stripBom(new TextDecoder('euc-kr').decode(bytes)), encoding: 'euc-kr' };
  }
}

/** A File's bytes (FileReader fallback for environments without Blob.arrayBuffer). */
export function readFileBytes(file: Blob): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === 'function') return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}

/** The columns each import kind reads, mirroring the backend's HEADER_ALIASES
 *  (unibridge-service app/services/network_import.py). `en`/`ko` list accepted
 *  header names; matching ignores case, spaces and `_ - . / ( )`. */
export interface CsvColumn {
  field: string;
  required: boolean;
  en: string[];
  ko: string[];
}

export const CSV_COLUMNS: Record<'hosts' | 'openings' | 'port_usages', CsvColumn[]> = {
  hosts: [
    { field: 'name', required: true, en: ['name', 'host', 'hostname'], ko: ['이름', '호스트', '호스트명', '서버', '서버명'] },
    { field: 'ip', required: true, en: ['ip', 'ip address', 'address'], ko: ['주소', 'IP주소', '아이피'] },
    { field: 'region', required: false, en: ['region', 'zone'], ko: ['리전', '지역', '존', '망'] },
    { field: 'owner', required: false, en: ['owner', 'team'], ko: ['담당자', '담당', '담당팀', '팀'] },
    { field: 'description', required: false, en: ['description', 'desc', 'note'], ko: ['설명', '비고', '용도'] },
    { field: 'kind', required: false, en: ['kind', 'hosttype'], ko: ['호스트구분', '호스트종류'] },
  ],
  openings: [
    { field: 'src', required: true, en: ['src', 'source', 'from', 'src ip', 'source ip'], ko: ['출발지', '출발', '출발지IP', '소스'] },
    { field: 'dst', required: true, en: ['dst', 'destination', 'to', 'dst ip', 'destination ip'], ko: ['목적지', '도착지', '목적지IP', '대상', '대상서버'] },
    { field: 'ports', required: true, en: ['ports', 'port', 'dst port'], ko: ['포트', '목적지포트', '서비스포트'] },
    { field: 'protocol', required: false, en: ['protocol', 'proto'], ko: ['프로토콜'] },
    { field: 'status', required: false, en: ['status', 'state'], ko: ['상태'] },
    { field: 'ticket_no', required: false, en: ['ticket no', 'ticket', 'request no'], ko: ['신청번호', '요청번호', '티켓', '티켓번호', '결재번호'] },
    { field: 'requester', required: false, en: ['requester', 'requested by'], ko: ['신청자', '요청자'] },
    { field: 'purpose', required: false, en: ['purpose', 'reason'], ko: ['용도', '목적', '사유', '설명', '비고'] },
  ],
  port_usages: [
    { field: 'host', required: true, en: ['host', 'hostname', 'name', 'ip'], ko: ['호스트', '호스트명', '서버', '서버명'] },
    { field: 'port', required: true, en: ['port'], ko: ['포트'] },
    { field: 'service_name', required: true, en: ['service name', 'service'], ko: ['서비스', '서비스명', '프로세스'] },
    { field: 'protocol', required: false, en: ['protocol', 'proto'], ko: ['프로토콜'] },
    { field: 'owner', required: false, en: ['owner', 'team'], ko: ['담당자', '담당', '팀'] },
    { field: 'description', required: false, en: ['description', 'desc', 'note'], ko: ['설명', '비고'] },
  ],
};

/** A header line to start a file from: the Korean or English names of every column. */
export function sampleHeader(kind: keyof typeof CSV_COLUMNS, lang: 'ko' | 'en'): string {
  return CSV_COLUMNS[kind].map((c) => (lang === 'ko' ? c.ko[0] : c.en[0])).join(',');
}
