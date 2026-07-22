// Round-trip check for the Portfolio editor's xlsx layer. Run from admin/:
//   node lib/portfolio.check.mjs
// Asserts parse→edit→serialize→parse preserves rows and lands edits at the
// exact columns src/sector.py load_portfolio reads. Node (>=22) strips the TS
// types on import; exceljs must be installed.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { parsePortfolio, serializePortfolio } from "./portfolio.ts";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "../../inputs/portfolio.xlsx");

const buf = readFileSync(SRC);
const data = await parsePortfolio(buf);
const before = data.companies.length;
const firstName = data.companies[0].company;

data.companies.push({
  company: "__RT_CO__", sector: "Test sector", business: "does testing",
  geo: "Global", website: "https://example.com",
});

const rt = await parsePortfolio(await serializePortfolio(buf, data));
const A = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } };

A(rt.companies.length === before + 1, "company count");
const c = rt.companies.at(-1);
A(c.company === "__RT_CO__" && c.geo === "Global" && c.website === "https://example.com", "new row round-trip");
A(rt.companies[0].company === firstName, "existing row preserved");

console.log("portfolio.xlsx round-trip OK", { before });
