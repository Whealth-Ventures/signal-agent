"""src/enricher.py — article bodies + tags for every story, for question answering.

Runs as its own process after the digest posts (deploy/run-digest.sh), so
nothing here can delay or block a digest. Two passes over agent.db:

  1. fetch: every story first seen in the window with no story_details row
     gets its article body (headline_rewriter's fetcher, larger cap). A failed
     fetch still gets a row (body_status='failed'), so it's never retried.
  2. tag: every fetched-but-untagged story goes to OpenAI in batches of
     BATCH_SIZE. Each batch is saved as it lands, so a failed call costs only
     that batch, and the next run picks it up.

Backfill: python src/enricher.py --days 30
Audit trail: data/logs/enrich_<date>.jsonl — one line per tag call + a summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI

import alerts
import config
import headline_rewriter
import storage
import topicality

BODY_MAX_CHARS = 20_000      # stored per story, ~3,500 words
TAG_EXCERPT_CHARS = 4_000    # of the body, sent to the tagger
BATCH_SIZE = 10
TAG_WORKERS = 4

MAGNITUDES = ("S", "A", "B", "C")
GEOS = ("India", "US", "Global")

# Labels per category: a story gets only the fields its own category's news
# has, so deal size lives in the deal categories and nowhere else. `event` is
# the same key everywhere (one query shape) with category-specific values.
# Kinds: tuple = allowed values, "usd" = number > 0, "list" = names, "text".
# A bucket key missing here (tuning.xlsx renamed one) just gets no facts.
CATEGORY_FIELDS: dict[str, dict[str, tuple[object, str]]] = {
    "venture_ipo": {
        "event": (("ipo", "ipo_filing", "funding_round", "fund_raise", "other"),
                  "ipo = pricing, open, or listed; ipo_filing = papers filed "
                  "(DRHP / S-1), no dates yet; fund_raise = a VC fund closing"),
        "round": ("text", 'e.g. "Seed", "Series B"'),
        "amount_usd": ("usd", "money raised in THIS deal, not earlier rounds"),
        "valuation_usd": ("usd", "valuation, if stated"),
        "investors": ("list", "lead investors first"),
        "exchange": ("text", "NSE, BSE, Nasdaq, NYSE (IPOs only)"),
    },
    "pe_strategics": {
        "event": (("acquisition", "buyout", "take_private", "stake", "platform",
                   "exit", "fund_raise", "other"), "stake = minority stake"),
        "buyer": ("text", "the acquirer or investor"),
        "target": ("text", "the company bought or invested in"),
        "amount_usd": ("usd", "deal value"),
    },
    "hospital_ma": {
        "event": (("acquisition", "merger", "affiliation", "divestiture", "closure", "other"), ""),
        "buyer": ("text", "the acquirer"),
        "target": ("text", "the hospital or system acquired"),
        "amount_usd": ("usd", "deal value"),
        "facilities": ("text", 'e.g. "3 hospitals, 450 beds"'),
    },
    "mso_rollups": {
        "event": (("acquisition", "platform_launch", "add_on", "recapitalization", "other"), ""),
        "platform": ("text", "the acquiring platform or MSO"),
        "target": ("text", "the practice acquired"),
        "specialty": ("text", 'e.g. "dermatology"'),
        "sponsor": ("text", "the PE backer"),
        "amount_usd": ("usd", "deal value"),
    },
    "fda_regulatory": {
        "event": (("approval", "rejection", "clearance", "label_change",
                   "warning_letter", "recall", "guidance", "other"), ""),
        "regulator": ("text", "FDA, CDSCO, EMA, ..."),
        "product": ("text", "drug or device name"),
        "indication": ("text", "what it treats"),
    },
    "hot_tas": {
        "event": (("trial_readout", "trial_start", "launch", "other"), ""),
        "therapy_area": ("text", 'e.g. "obesity / GLP-1", "oncology"'),
        "drug": ("text", "drug name"),
        "trial_phase": (("phase_1", "phase_2", "phase_3", "other"), ""),
        "outcome": (("positive", "negative", "mixed", "pending"), "trial result"),
    },
    "us_medicare": {
        "event": (("final_rule", "proposed_rule", "payment_update", "legislation",
                   "enforcement", "other"), ""),
        "agency": ("text", "CMS, HHS, Congress, ..."),
        "program": ("text", 'e.g. "Medicare Advantage", "Part D"'),
        "effective_date": ("text", "YYYY-MM-DD, if stated"),
    },
    "ai_healthcare": {
        "event": (("product_launch", "partnership", "deployment", "research", "policy", "other"), ""),
        "use_case": (("clinical_documentation", "revenue_cycle_admin", "diagnostics",
                      "drug_discovery", "patient_engagement", "genai_llm", "other"), ""),
        "product": ("text", "product or model name"),
        "customers": ("list", "health systems or payers adopting it"),
    },
}

# USD per 1M tokens (input, output), for the audit log only. A model missing
# here logs est_cost_usd: null rather than a wrong number.
_PRICES = {"gpt-4.1-mini": (0.40, 1.60)}


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path: Path = config.LOGS_DIR / f"enrich_{_today_str()}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


OTHER_HEALTHCARE, NOT_HEALTHCARE = "other_healthcare", "not_healthcare"


def _categories() -> tuple[str, ...]:
    return tuple(b.key for b in config.PRIORITY_BUCKETS) + (OTHER_HEALTHCARE, NOT_HEALTHCARE)


def _field_line(name: str, kind: object, note: str) -> str:
    shape = {"usd": "number in USD", "list": "list of names", "text": "short text"}
    k = " | ".join(kind) if isinstance(kind, tuple) else shape[kind]
    return f"      - {name} ({k})" + (f": {note}" if note else "")


def _system_prompt() -> str:
    blocks = []
    for b in config.PRIORITY_BUCKETS:
        fields = CATEGORY_FIELDS.get(b.key, {})
        lines = [_field_line(n, k, note) for n, (k, note) in fields.items()]
        blocks.append(f"  - {b.key}: {b.display}\n" + "\n".join(lines))
    blocks.append(f"  - {OTHER_HEALTHCARE}: healthcare news that fits none of the above. No facts.")
    blocks.append(f"  - {NOT_HEALTHCARE}: only when `healthcare` is false. No facts.")
    return (
        f"{config.TAGGER_SYSTEM_PROMPT}\n\n"
        "Categories, each with the facts to extract for it:\n" + "\n".join(blocks)
        + f"\n\nMagnitude rubric:\n{config.MAGNITUDE_RUBRIC}"
    )


def _clean_value(kind: object, v: object) -> object:
    if isinstance(kind, tuple):
        return v if v in kind else None
    if kind == "usd":
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        return float(v) if ok else None
    if kind == "list":
        return [str(x).strip() for x in v if str(x).strip()][:10] if isinstance(v, list) else None
    return str(v).strip()[:120] if isinstance(v, str) and v.strip() else None


def _clean(t: dict, text: str = "") -> dict:
    """One LLM tag entry → validated values. Anything off-list becomes a safe
    default rather than failing the story; facts outside the story's own
    category's fields are dropped.

    Healthcare needs both the model's yes AND the digest's own lexicon gate
    (topicality.py) on the story text: in the first live run the model filed
    26 non-healthcare IPOs (NSE, steel, fintech) under venture_ipo, and the
    lexicon caught 25 of them. A non-healthcare story is tier C, so "biggest"
    questions never surface it."""
    def pick(v, allowed):
        return v if v in allowed else None

    healthcare = t.get("healthcare") is True and topicality.is_healthcare(text)
    category = pick(t.get("category"), _categories()) or OTHER_HEALTHCARE
    if not healthcare:
        category = NOT_HEALTHCARE
    elif category == NOT_HEALTHCARE:  # model said healthcare, then contradicted itself
        category = OTHER_HEALTHCARE
    raw = t.get("facts") if isinstance(t.get("facts"), dict) else {}
    facts = {}
    for name, (kind, _) in CATEGORY_FIELDS.get(category, {}).items():
        v = _clean_value(kind, raw.get(name))
        if v not in (None, []):
            facts[name] = v
    comps = t.get("companies")
    return {
        "category": category,
        "facts": facts,
        "magnitude": "C" if category == NOT_HEALTHCARE else pick(t.get("magnitude"), MAGNITUDES),
        "companies": [
            str(c).strip() for c in (comps if isinstance(comps, list) else [])
            if str(c).strip()
        ][:5],
        "geo": pick(t.get("geo"), GEOS),
        "summary": str(t.get("summary") or "").strip()[:400] or None,
    }


def _tag_batch(client, system: str, rows: list[dict]) -> tuple[dict[str, dict], dict]:
    """One call → ({story_id: tags}, usage). Never raises: a failed batch
    returns no tags, so its stories stay untagged for the next run."""
    items = [
        {
            "id": r["id"], "title": r["title"], "summary": r["summary"] or "",
            "body": (r["body"] or "")[:TAG_EXCERPT_CHARS],
        }
        for r in rows
    ]
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=config.ENRICH_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"stories": items}, ensure_ascii=False)},
            ],
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        entries = parsed.get("stories") if isinstance(parsed, dict) else None
        texts = {it["id"]: f'{it["title"]} {it["summary"]} {it["body"]}' for it in items}
        tags = {
            str(e.get("id")): _clean(e, texts.get(str(e.get("id")), ""))
            for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict)
        }
        u = resp.usage
        usage = {"in": u.prompt_tokens if u else 0, "out": u.completion_tokens if u else 0}
        return tags, {**usage, "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # one bad batch must not cost the rest of the run
        return {}, {"in": 0, "out": 0, "error": f"{type(e).__name__}: {e}"[:500],
                    "problem": alerts.openai_problem(e)}


def _fetch_bodies(urls: list[str]) -> dict[str, str]:
    return asyncio.run(headline_rewriter._fetch_excerpts(urls, limit=BODY_MAX_CHARS))


def run(*, days: int, conn, client, fetch=_fetch_bodies) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    todo = storage.stories_without_details(since=since, conn=conn)
    bodies = fetch([url for _, url in todo]) if todo else {}
    for sid, url in todo:
        storage.save_story_body(sid, bodies.get(url, ""), conn=conn)
    conn.commit()

    rows = storage.untagged_stories(since=since, conn=conn)
    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    system = _system_prompt()
    stats = {
        "fetched": len(todo), "bodies_ok": sum(1 for _, u in todo if bodies.get(u)),
        "to_tag": len(rows), "tagged": 0, "failed_calls": 0, "in": 0, "out": 0,
        "openai_problem": None,
    }
    # Calls run in threads; every DB write stays on this thread.
    with ThreadPoolExecutor(TAG_WORKERS) as ex:
        results = ex.map(lambda b: _tag_batch(client, system, b), batches)
        for batch, (tags, usage) in zip(batches, results):
            ids = {r["id"] for r in batch}
            saved = 0
            for sid, t in tags.items():
                if sid in ids:  # ignore ids the model invented
                    storage.save_story_tags(sid, t, model=config.ENRICH_MODEL, conn=conn)
                    saved += 1
            conn.commit()
            stats["tagged"] += saved
            stats["failed_calls"] += 1 if "error" in usage else 0
            stats["openai_problem"] = stats["openai_problem"] or usage.get("problem")
            stats["in"] += usage["in"]
            stats["out"] += usage["out"]
            _log({"event": "tag_call", "model": config.ENRICH_MODEL,
                  "stories": len(batch), "tagged": saved, **usage})

    price = _PRICES.get(config.ENRICH_MODEL)
    stats["est_cost_usd"] = (
        round((stats["in"] * price[0] + stats["out"] * price[1]) / 1e6, 4)
        if price else None
    )
    _log({"event": "run_done", "days": days, **stats})
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch article bodies and tag stories for Q&A.")
    p.add_argument("--days", type=int, default=2,
                   help="Stories first seen in the last N days (backfill: --days 30).")
    p.add_argument("--geo", default="both", choices=["india", "us", "both"],
                   help="Which digest run this follows: picks the channel for an alert.")
    args = p.parse_args(argv)
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=config.OPENAI_API_KEY, max_retries=3, timeout=120)
    conn = storage.connect()
    try:
        storage.init_db(conn=conn)
        stats = run(days=args.days, conn=conn, client=client)
    finally:
        conn.close()
    print(f"enrich: {json.dumps(stats)}")
    if stats["openai_problem"]:
        channel = {"india": config.SLACK_CHANNEL_ID_INDIA,
                   "us": config.SLACK_CHANNEL_ID_US}.get(args.geo, config.SLACK_CHANNEL_ID)
        alerts.post_openai_alert(
            stats["openai_problem"], channel_id=channel or None,
            impact="Today's digest posted, but story labelling for the archive failed.",
        )
    return 1 if stats["to_tag"] and not stats["tagged"] else 0


if __name__ == "__main__":
    sys.exit(main())
