// Round-trip check for the Keywords editor's xlsx layer. Run from admin/:
//   node lib/keywords.check.mjs
// Asserts parse→edit→serialize→parse preserves rows and lands them at the exact
// columns (A-D from row 2) the agent's load_keywords() reads positionally.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import ExcelJS from "exceljs";
import { parseKeywords, serializeKeywords } from "./keywords.ts";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "../../inputs/keywords.xlsx");
const A = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } };

const buf = readFileSync(SRC);
const { rows } = await parseKeywords(buf);
A(rows.length > 2000, `expected >2000 keywords, got ${rows.length}`);
const first = rows[0];

rows.push({ bucket: "__RT_B__", subBucket: "__RT_S__", keyword: "__rt_kw__", geo: "US" });
const out = await serializeKeywords({ rows });

// Re-parse through our own layer.
const rt = (await parseKeywords(out)).rows;
A(rt.length === rows.length, "row count preserved");
A(rt[0].bucket === first.bucket && rt[0].keyword === first.keyword, "existing row preserved");
const last = rt.at(-1);
A(last.keyword === "__rt_kw__" && last.geo === "US", "new row round-trip");

// Verify the raw grid the agent reads: header row 1, data cols A-D from row 2.
const wb = new ExcelJS.Workbook();
await wb.xlsx.load(out);
const ws = wb.getWorksheet("Master Keywords");
A(ws, "Master Keywords sheet exists");
const h = ws.getRow(1);
A(String(h.getCell(1).value) === "Bucket" && String(h.getCell(3).value) === "Keyword", "header layout");
const r2 = ws.getRow(2);
A(String(r2.getCell(3).value) === first.keyword, "keyword in column C, row 2");
A(String(r2.getCell(4).value) === first.geo, "geo in column D, row 2");

console.log(`OK: ${rt.length} keywords round-trip, positional columns intact`);
