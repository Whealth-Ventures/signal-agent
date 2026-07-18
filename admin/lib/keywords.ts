import ExcelJS from "exceljs";

// Mirrors src/query_planner.py load_keywords(): single "Master Keywords" sheet,
// header row 1 (Bucket / Sub-bucket / Keyword / Geo), data from row 2. The agent
// reads columns positionally (A-D), so serialize must keep that exact layout.

export type KeywordRow = {
  bucket: string;
  subBucket: string;
  keyword: string;
  geo: string; // "India" | "US" | "Both" (agent normalizes unknown -> Both)
};

const SHEET = "Master Keywords";
const HEADER = ["Bucket", "Sub-bucket", "Keyword", "Geo"];

export async function parseKeywords(buf: Buffer): Promise<{ rows: KeywordRow[] }> {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf as any);
  const ws = wb.getWorksheet(SHEET);
  if (!ws) throw new Error(`keywords.xlsx missing sheet '${SHEET}'`);

  const s = (v: unknown) => (v == null ? "" : String(v).trim());
  const rows: KeywordRow[] = [];
  ws.eachRow((row, n) => {
    if (n === 1) return; // header
    const bucket = s(row.getCell(1).value);
    const subBucket = s(row.getCell(2).value);
    const keyword = s(row.getCell(3).value);
    const geo = s(row.getCell(4).value);
    // Agent skips rows with no keyword; do the same so blanks don't accumulate.
    if (!keyword && !bucket && !subBucket) return;
    rows.push({ bucket, subBucket, keyword, geo: normGeo(geo) });
  });
  return { rows };
}

export function normGeo(v: string): string {
  const g = v.trim().toLowerCase();
  if (g === "india") return "India";
  if (g === "us") return "US";
  return "Both";
}

export async function serializeKeywords(data: { rows: KeywordRow[] }): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet(SHEET);
  ws.addRow(HEADER);
  ws.getRow(1).font = { bold: true };
  ws.getRow(1).fill = {
    type: "pattern",
    pattern: "solid",
    fgColor: { argb: "FFE2E8F0" },
  };
  ws.views = [{ state: "frozen", ySplit: 1 }];
  ws.columns = [{ width: 34 }, { width: 30 }, { width: 46 }, { width: 10 }] as any;
  for (const r of data.rows) {
    const kw = (r.keyword || "").trim();
    if (!kw) continue; // never write a keyword-less row — the agent would drop it anyway
    ws.addRow([(r.bucket || "").trim(), (r.subBucket || "").trim(), kw, normGeo(r.geo || "")]);
  }
  const ab = await wb.xlsx.writeBuffer();
  return Buffer.from(ab as any);
}
