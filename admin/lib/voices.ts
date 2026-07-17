import ExcelJS from "exceljs";

// Sources editor for inputs/voices.xlsx.
//
// CRITICAL: the agent's loaders in src/query_planner.py read this workbook BY
// COLUMN POSITION (not by header name), with fixed banner/header row offsets per
// tab. So parse and serialize MUST agree on the exact same offsets, and
// serialize MUST leave the banner/header rows above the data untouched. We do
// that by mutating the *existing* workbook in place (rewriting only the data
// region), never rebuilding from scratch. The offset table below mirrors
// load_voices / load_newsletters / load_company_pages / load_firm_additions.

export type Publication = {
  name: string; geography: string; type: string; author: string;
  description: string; reach: string; url: string;
};
export type Voice = {
  name: string; category: string; subDomain: string; role: string;
  why: string; reach: string; linkedinUrl: string;
  tier: number | null; rssUrl: string;
};
export type FirmPage = {
  name: string; geography: string; type: string;
  description: string; whyFollow: string; linkedinUrl: string;
};
export type NewAddition = {
  category: string; firm: string; hq: string; stageTicket: string;
  thesis: string; portfolio: string; source: string;
};

export type VoicesData = {
  publications: Publication[];
  indiaVoices: Voice[];
  usVoices: Voice[];
  firms: FirmPage[];
  newAdditions: NewAddition[];
};

// Per-tab spec. `fields` lists the object keys in column order starting at
// column A. `index` means "column A is a regenerated row number, not a data
// field" (so parse skips it and serialize writes 1..n there). `primary` is the
// field that must be non-empty for a row to count. `dataStartRow` is 1-indexed.
type Spec = {
  key: keyof VoicesData;
  sheet: string;
  dataStartRow: number;
  index: boolean;
  fields: string[];
  numberFields?: string[];
  primary: string;
};

const SPECS: Spec[] = [
  {
    key: "publications", sheet: "Newsletters & Publications", dataStartRow: 3,
    index: true, primary: "name",
    fields: ["name", "geography", "type", "author", "description", "reach", "url"],
  },
  {
    key: "indiaVoices", sheet: "India Top Voices", dataStartRow: 4,
    index: true, primary: "name", numberFields: ["tier"],
    fields: ["name", "category", "subDomain", "role", "why", "reach", "linkedinUrl", "tier", "rssUrl"],
  },
  {
    key: "usVoices", sheet: "US Top Voices", dataStartRow: 4,
    index: true, primary: "name", numberFields: ["tier"],
    fields: ["name", "category", "subDomain", "role", "why", "reach", "linkedinUrl", "tier", "rssUrl"],
  },
  {
    key: "firms", sheet: "Firms & Org Pages", dataStartRow: 3,
    index: true, primary: "name",
    fields: ["name", "geography", "type", "description", "whyFollow", "linkedinUrl"],
  },
  {
    key: "newAdditions", sheet: "New Additions", dataStartRow: 3,
    index: false, primary: "firm",
    fields: ["category", "firm", "hq", "stageTicket", "thesis", "portfolio", "source"],
  },
];

// exceljs cell values come in several shapes (string, number, richText,
// hyperlink object, formula result). Flatten to a trimmed string.
function cellStr(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v.trim();
  if (typeof v === "number") return String(v);
  if (typeof v === "object") {
    const o = v as any;
    if (typeof o.text === "string") return o.text.trim();          // hyperlink
    if (Array.isArray(o.richText)) return o.richText.map((r: any) => r.text).join("").trim();
    if (o.result != null) return String(o.result).trim();          // formula
  }
  return String(v).trim();
}

function cellNum(v: unknown): number | null {
  const s = cellStr(v);
  if (!s) return null;
  const n = Number(s);
  return Number.isNaN(n) ? null : n;
}

export async function parseVoices(buf: Buffer): Promise<VoicesData> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf as any);
  const out: any = {};
  for (const spec of SPECS) {
    const ws = wb.getWorksheet(spec.sheet);
    const rows: any[] = [];
    if (ws) {
      for (let r = spec.dataStartRow; r <= ws.rowCount; r++) {
        const row = ws.getRow(r);
        const obj: any = {};
        // Column A is either the # index (skip) or the first data field.
        let col = spec.index ? 2 : 1;
        for (const f of spec.fields) {
          const raw = row.getCell(col++).value;
          obj[f] = spec.numberFields?.includes(f) ? cellNum(raw) : cellStr(raw);
        }
        if (cellStr(obj[spec.primary])) rows.push(obj);
      }
    }
    out[spec.key] = rows;
  }
  return out as VoicesData;
}

// Rewrite the data region of each tab in place on the existing workbook,
// preserving the banner/header rows above `dataStartRow` and any styling.
export async function serializeVoices(buf: Buffer, data: VoicesData): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf as any);
  // Widest tab is 10 columns (voices, A..J); clear a bit past that to be safe.
  const CLEAR_WIDTH = 12;
  for (const spec of SPECS) {
    const ws = wb.getWorksheet(spec.sheet);
    if (!ws) continue; // tab absent (e.g. New Additions) → leave untouched
    const originalLast = ws.rowCount; // capture before getRow() extends it
    const rows = ((data[spec.key] as any[]) || []).filter((o) => cellStr(o[spec.primary]));
    // Overwrite the data region in place, row by row, from dataStartRow down.
    // (spliceRows proved unreliable across exceljs versions, so we don't use it.)
    let r = spec.dataStartRow;
    for (const obj of rows) {
      const row = ws.getRow(r);
      let col = 1;
      if (spec.index) row.getCell(col++).value = r - spec.dataStartRow + 1;
      for (const f of spec.fields) {
        const v = obj[f];
        const cell = row.getCell(col++);
        if (v == null || v === "") cell.value = null;
        else cell.value = spec.numberFields?.includes(f) ? Number(v) : String(v);
      }
      r++;
    }
    // Blank out any leftover old rows below the new data.
    for (; r <= originalLast; r++) {
      const row = ws.getRow(r);
      for (let c = 1; c <= CLEAR_WIDTH; c++) row.getCell(c).value = null;
    }
  }
  const ab = await wb.xlsx.writeBuffer();
  return Buffer.from(ab as any);
}
