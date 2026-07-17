// Round-trip check for the Sources editor's xlsx layer. Run from admin/:
//   node lib/voices.check.mjs
// Asserts that parse→edit→serialize→parse preserves every tab and lands the
// edited row at the exact columns the agent's positional loaders read. Node
// (>=22) strips the TS types on import; exceljs must be installed.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { parseVoices, serializeVoices } from "./voices.ts";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "../../inputs/voices.xlsx");

const buf = readFileSync(SRC);
const data = await parseVoices(buf);
const before = Object.fromEntries(Object.entries(data).map(([k, v]) => [k, v.length]));

data.publications.push({
  name: "__RT_PUB__", geography: "India", type: "Trade", author: "ET",
  description: "deals", reach: "high", url: "https://example.com/feed",
});
data.indiaVoices.push({
  name: "__RT_VOICE__", category: "Investor", subDomain: "VC", role: "Partner",
  why: "t", reach: "high", linkedinUrl: "https://x.test", tier: 1, rssUrl: "",
});

const rt = await parseVoices(await serializeVoices(buf, data));
const A = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } };

A(rt.publications.length === before.publications + 1, "pub count");
A(rt.indiaVoices.length === before.indiaVoices + 1, "india voice count");
A(rt.usVoices.length === before.usVoices, "us voices untouched");
A(rt.firms.length === before.firms, "firms untouched");
A(rt.newAdditions.length === before.newAdditions, "new additions untouched");

const p = rt.publications.at(-1);
A(p.name === "__RT_PUB__" && p.url === "https://example.com/feed", "pub round-trip");
const v = rt.indiaVoices.at(-1);
A(v.name === "__RT_VOICE__" && v.tier === 1, "voice tier round-trip (number)");
A(rt.publications[0].name === data.publications[0].name, "existing row preserved");

console.log("voices.xlsx round-trip OK", before);
