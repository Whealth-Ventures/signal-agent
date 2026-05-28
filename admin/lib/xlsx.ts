import ExcelJS from "exceljs";

// Shape mirrors src/tunables.py in the agent repo. Keep these in sync — when
// you add a new sheet or column there, mirror it here.

export type SettingRow = { name: string; value: string | number | null; description: string };
export type BoosterRow = { name: string; weight: number; pattern_regex: string; description: string };
export type PriorityBucketRow = { key: string; display: string; sub_buckets: string; geos: string };
export type SourceTierRow = { host: string };

export type Tuning = {
  settings: SettingRow[];
  boosters: BoosterRow[];
  priorityBuckets: PriorityBucketRow[];
  sourceTiers: SourceTierRow[];
};

function readRows<T extends Record<string, unknown>>(
  ws: ExcelJS.Worksheet,
  columns: (keyof T)[],
): T[] {
  const out: T[] = [];
  const header = ws.getRow(1);
  const colIndex = new Map<keyof T, number>();
  header.eachCell((cell, idx) => {
    const name = String(cell.value || "").trim() as keyof T;
    if (columns.includes(name)) colIndex.set(name, idx);
  });
  for (const c of columns) {
    if (!colIndex.has(c)) {
      throw new Error(`Sheet '${ws.name}' missing column '${String(c)}'`);
    }
  }
  ws.eachRow((row, rowNum) => {
    if (rowNum === 1) return;
    const obj: Record<string, unknown> = {};
    let hasContent = false;
    for (const c of columns) {
      const idx = colIndex.get(c)!;
      const raw = row.getCell(idx).value;
      let v: string | number | null = null;
      if (raw === null || raw === undefined) v = null;
      else if (typeof raw === "number") v = raw;
      else if (typeof raw === "string") v = raw.trim() || null;
      else v = String(raw);
      obj[c as string] = v;
      if (v !== null && v !== "") hasContent = true;
    }
    if (hasContent) out.push(obj as T);
  });
  return out;
}

export async function parseTuning(buf: Buffer): Promise<Tuning> {
  const wb = new ExcelJS.Workbook();
  // exceljs declares its own local `Buffer extends ArrayBuffer` interface,
  // which doesn't structurally match Node's Buffer. Runtime accepts both.
  await wb.xlsx.load(buf as any);

  const required = ["Settings", "Boosters", "Priority Buckets", "Source Tiers"];
  for (const name of required) {
    if (!wb.getWorksheet(name)) {
      throw new Error(`tuning.xlsx missing sheet '${name}'`);
    }
  }

  const settings = readRows<SettingRow>(
    wb.getWorksheet("Settings")!,
    ["name", "value", "description"],
  );
  const boostersRaw = readRows<{
    name: string;
    weight: number | string | null;
    pattern_regex: string | null;
    description: string;
  }>(
    wb.getWorksheet("Boosters")!,
    ["name", "weight", "pattern_regex", "description"],
  );
  const boosters: BoosterRow[] = boostersRaw.map((b) => ({
    name: String(b.name),
    weight: typeof b.weight === "number" ? b.weight : Number(b.weight),
    pattern_regex: b.pattern_regex == null ? "" : String(b.pattern_regex),
    description: String(b.description || ""),
  }));
  const priorityBuckets = readRows<PriorityBucketRow>(
    wb.getWorksheet("Priority Buckets")!,
    ["key", "display", "sub_buckets", "geos"],
  ).map((p) => ({
    key: String(p.key || ""),
    display: String(p.display || ""),
    sub_buckets: String(p.sub_buckets || ""),
    geos: String(p.geos || ""),
  }));
  const sourceTiers = readRows<SourceTierRow>(
    wb.getWorksheet("Source Tiers")!,
    ["host"],
  ).map((s) => ({ host: String(s.host || "") }));

  return { settings, boosters, priorityBuckets, sourceTiers };
}

export async function serializeTuning(t: Tuning): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();

  const header = (ws: ExcelJS.Worksheet, cols: string[]) => {
    ws.addRow(cols);
    ws.getRow(1).font = { bold: true };
    ws.getRow(1).fill = {
      type: "pattern",
      pattern: "solid",
      fgColor: { argb: "FFE2E8F0" },
    };
    ws.views = [{ state: "frozen", ySplit: 1 }];
  };

  const settings = wb.addWorksheet("Settings");
  header(settings, ["name", "value", "description"]);
  settings.columns = [
    { width: 35 }, { width: 25 }, { width: 90 },
  ] as any;
  for (const r of t.settings) {
    settings.addRow([r.name, r.value, r.description]);
  }

  const boosters = wb.addWorksheet("Boosters");
  header(boosters, ["name", "weight", "pattern_regex", "description"]);
  boosters.columns = [
    { width: 24 }, { width: 10 }, { width: 55 }, { width: 80 },
  ] as any;
  for (const r of t.boosters) {
    boosters.addRow([
      r.name,
      r.weight,
      r.pattern_regex || null,
      r.description,
    ]);
  }

  const pb = wb.addWorksheet("Priority Buckets");
  header(pb, ["key", "display", "sub_buckets", "geos"]);
  pb.columns = [
    { width: 20 }, { width: 38 }, { width: 90 }, { width: 16 },
  ] as any;
  for (const r of t.priorityBuckets) {
    pb.addRow([r.key, r.display, r.sub_buckets, r.geos]);
  }

  const tiers = wb.addWorksheet("Source Tiers");
  header(tiers, ["host"]);
  tiers.columns = [{ width: 42 }] as any;
  for (const r of t.sourceTiers) {
    tiers.addRow([r.host]);
  }

  const ab = await wb.xlsx.writeBuffer();
  return Buffer.from(ab as any);
}
