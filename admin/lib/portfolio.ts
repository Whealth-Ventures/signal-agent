import ExcelJS from "exceljs";

// Portfolio editor for inputs/portfolio.xlsx (the Sector Agent's input).
//
// The agent's loader (src/sector.py `load_portfolio`) reads the `Portfolio`
// sheet BY COLUMN POSITION: row 1 is the header, data starts at row 2, columns
// A-E are Company, Sector, What they do, Geo, Website. So parse/serialize must
// agree on that layout, and serialize rewrites only the data region (row 2+),
// leaving the header row and styling untouched — same in-place approach as
// lib/voices.ts.

export type Company = {
  company: string;
  sector: string;
  business: string;
  geo: string;
  website: string;
};

export type PortfolioData = { companies: Company[] };

const SHEET = "Portfolio";
const DATA_START_ROW = 2; // row 1 = header
const FIELDS: (keyof Company)[] = ["company", "sector", "business", "geo", "website"];
const PRIMARY: keyof Company = "company";
const CLEAR_WIDTH = 8;

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

export async function parsePortfolio(buf: Buffer): Promise<PortfolioData> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf as any);
  const ws = wb.getWorksheet(SHEET);
  const companies: Company[] = [];
  if (ws) {
    for (let r = DATA_START_ROW; r <= ws.rowCount; r++) {
      const row = ws.getRow(r);
      const obj: any = {};
      let col = 1;
      for (const f of FIELDS) obj[f] = cellStr(row.getCell(col++).value);
      if (cellStr(obj[PRIMARY])) companies.push(obj as Company);
    }
  }
  return { companies };
}

// Rewrite the data region in place, preserving the header row and styling.
export async function serializePortfolio(buf: Buffer, data: PortfolioData): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf as any);
  const ws = wb.getWorksheet(SHEET);
  if (!ws) throw new Error(`Sheet "${SHEET}" not found in portfolio.xlsx`);

  const originalLast = ws.rowCount; // capture before getRow() extends it
  const rows = (data.companies || []).filter((o) => cellStr(o[PRIMARY]));

  let r = DATA_START_ROW;
  for (const obj of rows) {
    const row = ws.getRow(r);
    let col = 1;
    for (const f of FIELDS) {
      const v = obj[f];
      const cell = row.getCell(col++);
      cell.value = v == null || v === "" ? null : String(v);
    }
    r++;
  }
  // Blank out any leftover old rows below the new data.
  for (; r <= originalLast; r++) {
    const row = ws.getRow(r);
    for (let c = 1; c <= CLEAR_WIDTH; c++) row.getCell(c).value = null;
  }
  const ab = await wb.xlsx.writeBuffer();
  return Buffer.from(ab as any);
}
