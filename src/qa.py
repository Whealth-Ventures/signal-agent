"""src/qa.py — answer questions from the labelled news archive (Phase B).

The brain behind src/bot.py, kept free of Slack so it can be tested on its
own. The model gets two fixed tools over agent.db, opened read-only, and never
writes SQL:

  search_stories  filter by category / event / geo / company / dates /
                  magnitude, keyword-match title + summaries + article body
  get_story       one story's labels and article text

Links are checked on the way out: a URL in the answer that no tool call
returned is unlinked, so the bot can't invent a citation.

Audit trail: data/logs/qa_<date>.jsonl, one line per question (src/bot.py).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
from openai import OpenAI

import config
import enricher
import storage

MAX_ROUNDS = 5
SEARCH_LIMIT_MAX = 25
BODY_CHARS_FOR_MODEL = 6_000
# USD per 1M tokens (input, output), for the audit log only.
_PRICES = {"gpt-4.1": (2.00, 8.00)}

# A filing and the IPO it leads to are one story arc for merging: Slack
# testing showed Electra's "sets terms" and "prices" stories as two items.
_EVENT_FAMILY = {"ipo_filing": "ipo"}
# Merging one event told by several outlets. Measured on prod on 29 Sept 2026
# (the digest's text-embedding-3-small vectors): real duplicates scored 0.65 to
# 0.89 (ADARx across outlets 0.84 and 0.89, Electra's filing vs pricing 0.73),
# while different news reached 0.72 ("Lilly obesity access" vs "Lilly
# pipeline"). No threshold alone separates those, so similarity must agree
# with the labels (same category and event family) and the dates.
MERGE_SIMILARITY = 0.70
MERGE_WINDOW_DAYS = 10

_SEARCH_ARGS = {
    "query", "category", "events", "geo", "company", "published_after",
    "published_before", "min_magnitude", "min_amount", "max_amount",
    "include_not_healthcare", "sort", "limit",
}

# A deal-size bound as the user said it; code converts it to USD, so the
# model never does currency maths (it once kept a $40M round under "up to
# ₹200 Cr").
_MONEY_BOUND = {"type": "object", "additionalProperties": False,
                "required": ["value", "unit", "currency"], "properties": {
                    "value": {"type": "number"},
                    "unit": {"type": "string", "enum": list(enricher.MONEY_UNITS)},
                    "currency": {"type": "string", "enum": list(enricher.PER_USD)}}}

TOOLS = [
    {"type": "function", "function": {
        "name": "search_stories",
        "description": (
            "Search the labelled healthcare news archive. Filters combine with AND. "
            "Returns up to `limit` events, most important first: title, url, published "
            "date, category, event, magnitude, geo, amount (as the article states it, "
            "with USD in brackets), amount_usd (for comparing), valuation, companies, a "
            "two-sentence summary, and more_links (other outlets covering the same event)."
        ),
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "description": (
                "1 to 3 words that must ALL appear in the story (company, drug, product "
                "or person names). Leave empty when the filters already express the question.")},
            "category": {"type": "string", "enum": list(enricher._categories())},
            "events": {"type": "array", "items": {"type": "string"}, "description": (
                "Any of these event values, e.g. [\"ipo\", \"ipo_filing\"] for IPO questions, "
                "[\"approval\", \"clearance\"] for approvals. Values per category are in your "
                "instructions. Leave empty to get every event in the category.")},
            "geo": {"type": "string", "enum": list(enricher.GEOS)},
            "company": {"type": "string", "description": "A company or organisation name, partial match."},
            "published_after": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
            "published_before": {"type": "string", "description": "YYYY-MM-DD, exclusive."},
            "min_magnitude": {"type": "string", "enum": list(enricher.MAGNITUDES), "description": (
                "S is biggest. 'A' returns S and A.")},
            "min_amount": {**_MONEY_BOUND, "description": (
                "Deal size at least this, in the user's own words and currency, e.g. "
                "{\"value\": 200, \"unit\": \"crore\", \"currency\": \"INR\"}. "
                "Stories with no stated amount are left out.")},
            "max_amount": {**_MONEY_BOUND, "description": (
                "Deal size at most this, same shape as min_amount. Stories with no "
                "stated amount are left out.")},
            "include_not_healthcare": {"type": "boolean", "description": (
                "Only when the user explicitly asks about non-healthcare news.")},
            "sort": {"type": "string", "enum": ["importance", "recent"], "description": (
                "importance = magnitude, then deal size (default); recent = newest first.")},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_story",
        "description": "One story's full labels and article text, by an id from search_stories.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["id"],
                       "properties": {"id": {"type": "string"}}},
    }},
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _usd_short(usd: float) -> str:
    """3 significant figures: $3.35B, $446M, $24.8M. Rounds up into the next
    unit ($999.6M is $1B), where .3g alone would print $1e+03M."""
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        v = float(f"{usd / size:.3g}")
        if v >= 1:
            return f"${v:g}{suffix}"
    return f"${usd:,.0f}"


def money_display(facts: dict, field: str = "amount") -> str | None:
    """"₹4,800 Cr (~$545M)": the article's own figure, plus USD for comparison.
    Labels made before the money change only have <field>_usd: "~$446M"."""
    usd, text = facts.get(f"{field}_usd"), facts.get(f"{field}_text")
    if not enricher.valid_usd(usd):
        return None
    if not text:
        return f"~{_usd_short(usd)}"
    # Dollars in one consistent form ($3.35B, not the model's $3,350M); any
    # other currency as the article wrote it, with USD alongside.
    return _usd_short(usd) if text.startswith("$") else f"{text} (~{_usd_short(usd)})"


def _row(r: sqlite3.Row) -> dict:
    facts = json.loads(r["facts"] or "{}")
    amount = facts.get("amount_usd")
    return {
        "id": r["id"], "title": r["title"], "url": r["url"],
        "published": (r["published_at"] or "")[:10], "category": r["category"],
        "event": facts.get("event"), "magnitude": r["magnitude"], "geo": r["geo"],
        "amount_usd": amount if enricher.valid_usd(amount) else None,
        "amount": money_display(facts), "valuation": money_display(facts, "valuation"),
        "companies": json.loads(r["companies"] or "[]"),
        "summary": r["summary"] or r["canonical_summary"] or "",
    }


_SELECT = """SELECT s.id, s.canonical_title AS title, s.canonical_url AS url, s.published_at,
       s.canonical_summary, d.category, d.facts, d.magnitude, d.geo, d.companies, d.summary
FROM story_details d JOIN stories s ON s.id = d.story_id"""


def search_stories(
    conn: sqlite3.Connection, *, query: str | None = None, category: str | None = None,
    events: list[str] | str | None = None, geo: str | None = None, company: str | None = None,
    published_after: str | None = None, published_before: str | None = None,
    min_magnitude: str | None = None, min_amount: dict | None = None,
    max_amount: dict | None = None, include_not_healthcare: bool = False,
    sort: str = "importance", limit: int = 15,
) -> list[dict]:
    """Filter values the model got wrong are ignored rather than failing the
    search, so a bad enum never costs the whole answer.

    Stories about the same event from different outlets are merged into one
    result (the most important one, with the others as more_links): in the
    first live test the model listed ADARx's IPO twice despite being told to
    merge. See _merge_same_event."""
    where, params = ["d.tagged_at IS NOT NULL"], []
    if category in enricher._categories():
        where.append("d.category = ?")
        params.append(category)
    if category != enricher.NOT_HEALTHCARE and not include_not_healthcare:
        where.append("d.category != ?")
        params.append(enricher.NOT_HEALTHCARE)
    events = [events] if isinstance(events, str) else [e for e in (events or []) if isinstance(e, str)]
    if events:
        where.append(f"json_extract(d.facts, '$.event') IN ({', '.join('?' * len(events))})")
        params += events
    if geo in enricher.GEOS:
        where.append("d.geo = ?")
        params.append(geo)
    if company:
        where.append("(d.companies LIKE ? OR s.canonical_title LIKE ?)")
        params += [f"%{company}%"] * 2
    # Parsed, not compared as raw strings: "2026/09/15" would silently match
    # nothing and "15 September 2026" would silently filter nothing.
    if (after := _iso_date(published_after)):
        where.append("s.published_at >= ?")
        params.append(after)
    if (before := _iso_date(published_before)):
        where.append("s.published_at < ?")
        params.append(before)
    if min_magnitude in enricher.MAGNITUDES:
        where.append("instr('SABC', d.magnitude) BETWEEN 1 AND ?")
        params.append("SABC".index(min_magnitude) + 1)
    for bound, op in ((min_amount, ">="), (max_amount, "<=")):
        if (money := enricher.parse_money(bound)):
            where.append(f"json_extract(d.facts, '$.amount_usd') BETWEEN ? AND ? "
                         f"AND json_extract(d.facts, '$.amount_usd') {op} ?")
            params += [enricher.MIN_DEAL_USD, enricher.MAX_DEAL_USD, money[0]]
    for term in (query or "").split()[:3]:
        where.append("(s.canonical_title LIKE ? OR d.summary LIKE ? "
                     "OR s.canonical_summary LIKE ? OR d.body LIKE ?)")
        params += [f"%{term}%"] * 4
    order = ("s.published_at DESC" if sort == "recent" else
             "COALESCE(instr('SABC', d.magnitude), 9), "
             "CASE WHEN json_extract(d.facts, '$.amount_usd') BETWEEN "
             f"{enricher.MIN_DEAL_USD} AND {enricher.MAX_DEAL_USD} "
             "THEN json_extract(d.facts, '$.amount_usd') END DESC, s.published_at DESC")
    limit = max(1, min(int(limit or 15), SEARCH_LIMIT_MAX))
    # Over-fetch so merging duplicates still leaves `limit` distinct events.
    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {limit * 4}"
    return _merge_same_event(conn, [_row(r) for r in conn.execute(sql, params)], limit)


def _iso_date(v: object) -> str | None:
    try:
        return date.fromisoformat(str(v).strip().replace("/", "-")[:10]).isoformat() if v else None
    except ValueError:
        return None


def _merge_same_event(conn: sqlite3.Connection, rows: list[dict], limit: int) -> list[dict]:
    """Greedy leader selection, as in ranker.collapse_near_duplicates: rows
    come most important first, each becomes a leader unless it matches an
    existing leader, and a match joins that leader's more_links. A story with
    no stored embedding is never merged."""
    vecs = {}
    for sid, v in storage.load_story_embeddings([r["id"] for r in rows], conn=conn).items():
        a = np.asarray(v, dtype=np.float32)
        if (n := float(np.linalg.norm(a))):
            vecs[sid] = a / n

    def same_event(a: dict, b: dict) -> bool:
        if a["category"] != b["category"] or a["id"] not in vecs or b["id"] not in vecs:
            return False
        if _EVENT_FAMILY.get(a["event"], a["event"]) != _EVENT_FAMILY.get(b["event"], b["event"]):
            return False
        try:
            gap = abs((date.fromisoformat(a["published"]) - date.fromisoformat(b["published"])).days)
        except ValueError:
            return False
        return gap <= MERGE_WINDOW_DAYS and float(vecs[a["id"]] @ vecs[b["id"]]) >= MERGE_SIMILARITY

    leaders: list[dict] = []
    for r in rows:
        lead = next((ld for ld in leaders if same_event(ld, r)), None)
        if lead:
            lead["more_links"].append(r["url"])
        elif len(leaders) < limit:
            leaders.append({**r, "more_links": []})
    return leaders


def get_story(conn: sqlite3.Connection, story_id: str) -> dict | None:
    r = conn.execute(f"{_SELECT.replace('d.summary', 'd.summary, d.body')} WHERE s.id = ?",
                     (story_id,)).fetchone()
    if r is None:
        return None
    out = _row(r)
    # Money only as the cleaned top-level fields: a raw facts copy would hand
    # the model a stored units slip such as amount_usd 350.
    out["facts"] = {k: v for k, v in json.loads(r["facts"] or "{}").items()
                    if not k.endswith(("_usd", "_text"))}
    out["article_text"] = (r["body"] or "")[:BODY_CHARS_FOR_MODEL] or "(article could not be fetched)"
    return out


def _dispatch(
    conn: sqlite3.Connection, name: str, args: dict, seen_urls: set[str], covers_from: str = "",
) -> str:
    try:
        if name == "search_stories":
            stories = search_stories(conn, **{k: v for k, v in args.items() if k in _SEARCH_ARGS})
        elif name == "get_story":
            story = get_story(conn, str(args.get("id", "")))
            stories = [story] if story else []
        else:
            return json.dumps({"error": f"unknown tool {name}"})
    except Exception as e:  # a bad argument costs one tool call, not the answer
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    seen_urls.update(u for s in stories for u in [s["url"], *s.get("more_links", [])])
    # On every result, not just the system prompt: in live tests the model
    # searched July, found nothing, and never said the archive starts later.
    return json.dumps({"archive_covers_from": covers_from, "stories": stories}, ensure_ascii=False)


def _coverage(conn: sqlite3.Connection) -> tuple[int, str]:
    """(labelled stories, first-seen date as "14 September 2026")."""
    n, first = conn.execute(
        "SELECT count(*), min(s.created_at) FROM story_details d "
        "JOIN stories s ON s.id = d.story_id WHERE d.tagged_at IS NOT NULL"
    ).fetchone()
    return n, (datetime.fromisoformat(first).strftime("%-d %B %Y") if first else "no date")


def _system_prompt(n: int, since: str) -> str:
    today = datetime.now(ZoneInfo(config.DIGEST_TZ_INDIA))
    today = f"{today:%A} {today.day} {today:%B %Y} (YYYY-MM-DD: {today:%Y-%m-%d})"
    events = "\n".join(
        f"  - {cat}: {', '.join(fields['event'][0])}"
        for cat, fields in enricher.CATEGORY_FIELDS.items() if "event" in fields
    )
    return (
        f"{config.QA_SYSTEM_PROMPT}\n\n"
        f"Today is {today}. The archive holds {n} labelled stories, first seen from "
        f"{since} onwards. Anything earlier is not in it.\n\n"
        f"Event values per category (for the `event` filter):\n{events}"
    )


_ANY_URL = re.compile(r"https?://[^\s|>)\]]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>")


def keep_known_links(text: str, allowed: set[str]) -> str:
    """Unlink every URL no tool returned, whatever its form: [text](url),
    <url|text> (the Slack form the bot's own history is in), <url>, (url) or
    bare. Link text survives; the URL doesn't. The model copies URLs from
    tool results, so a miss is invented."""
    def ok(url: str) -> bool:
        return url.rstrip(".,;:!?") in allowed

    text = _MD_LINK.sub(lambda m: m.group(0) if ok(m.group(2)) else m.group(1), text)
    text = _SLACK_LINK.sub(lambda m: m.group(0) if ok(m.group(1)) else (m.group(2) or ""), text)

    def bare(m: re.Match) -> str:  # anything left, in any wrapping
        url = m.group(0)
        core = url.rstrip(".,;:!?")
        return url if core in allowed else url[len(core):]
    text = _ANY_URL.sub(bare, text)
    return re.sub(r"\(\s*\)|<\s*>", "", text)


_CLOSING_OFFER = re.compile(
    r"(?:(?:\n\s*)+|(?<=[.!?])[ \t]+)"  # its own line, or the last paragraph's last sentence
    r"(Let me know|If you'?d like|If you want|Would you like|Feel free)[^\n]*\s*$", re.IGNORECASE)


def _house_style(text: str) -> str:
    """No em-dashes and no closing "Let me know if…" line in team-facing text;
    the model slips on both despite the prompt."""
    text = re.sub(r"\s*\u2014\s*", ", ", text)
    return _CLOSING_OFFER.sub("", text).rstrip()


def answer(question: str, history: list[dict] | None = None, *, client=None) -> tuple[str, dict]:
    """(answer text in markdown, usage). Raises on an OpenAI failure so the
    caller can tell the user; tool errors are handled inside the loop."""
    client = client or OpenAI(api_key=config.OPENAI_API_KEY, max_retries=3, timeout=90)
    usage = {"rounds": 0, "tool_calls": 0, "in": 0, "out": 0}
    # Links in the bot's own earlier answers came from earlier searches, so a
    # follow-up may reuse them. Not links people posted: those aren't vetted.
    seen = {u.rstrip(".,;:!?") for m in (history or []) if m.get("role") == "assistant"
            for u in _ANY_URL.findall(m.get("content") or "")}
    conn = _connect()
    try:
        n, since = _coverage(conn)
        messages = [{"role": "system", "content": _system_prompt(n, since)},
                    *(history or []), {"role": "user", "content": question}]
        for i in range(MAX_ROUNDS):
            resp = client.chat.completions.create(
                model=config.QA_MODEL, messages=messages, tools=TOOLS, temperature=0.2,
                # Round 1 must search: follow-ups otherwise get answered from the
                # thread, which in live tests was incomplete.
                tool_choice="required" if i == 0 else "auto",
            )
            usage["rounds"] += 1
            if resp.usage:
                usage["in"] += resp.usage.prompt_tokens
                usage["out"] += resp.usage.completion_tokens
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return _house_style(keep_known_links(msg.content or "", seen)), _priced(usage)
            messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]})
            for tc in msg.tool_calls:
                usage["tool_calls"] += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                usage.setdefault("searches", []).append({"tool": tc.function.name, **args})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": _dispatch(conn, tc.function.name, args, seen, since)})
        return ("I searched but couldn't settle on an answer. Try narrowing it to a "
                "company, a category or a date range."), _priced(usage)
    finally:
        conn.close()


def _priced(usage: dict) -> dict:
    p = _PRICES.get(config.QA_MODEL)
    usage["est_cost_usd"] = round((usage["in"] * p[0] + usage["out"] * p[1]) / 1e6, 4) if p else None
    return usage
