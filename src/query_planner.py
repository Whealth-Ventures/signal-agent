"""Layer 1: deterministic query planner.

Reads inputs/keywords.xlsx and inputs/voices.xlsx, emits ~32 QueryPlan objects
clustered by (geography, bucket). Pure functions; no I/O beyond reading inputs/.
Same Excel input always produces the same plans, byte-for-byte.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openpyxl import load_workbook

import config

Geography = Literal["India", "US", "Global"]

KEYWORDS_PER_SUB_BUCKET_IN_PROMPT = 3

_TAB_TO_GEOGRAPHY: dict[str, Geography] = {
    "India Keywords": "India",
    "US Keywords": "US",
    "Cross-Cutting Themes": "Global",
}

_GEO_LABEL: dict[Geography, str] = {
    "India": "India",
    "US": "the United States",
    "Global": "globally (cross-cutting themes)",
}

_GEO_ORDER: list[Geography] = ["India", "US", "Global"]


# --- Dataclasses --------------------------------------------------------

@dataclass(frozen=True)
class KeywordRow:
    geography: Geography
    bucket: str
    sub_bucket: str
    keyword: str


@dataclass(frozen=True)
class Voice:
    tier: int | None
    name: str
    category: str
    sub_domain: str
    role: str
    why: str
    channels: str
    confidence: str
    url_status: str
    linkedin_url: str
    geography: Literal["India", "US"]


@dataclass(frozen=True)
class Newsletter:
    tier: int | None
    name: str
    geography: str
    type_: str
    author: str
    description: str
    reach: str
    url: str


@dataclass(frozen=True)
class CompanyPage:
    geography: str
    name: str
    type_: str
    description: str
    why_follow: str
    linkedin_url: str


@dataclass(frozen=True)
class QueryPlan:
    id: str
    geography: Geography
    bucket: str
    sub_buckets: tuple[str, ...]
    keyword_sample: tuple[str, ...]
    keyword_count_total: int
    prompt_text: str
    voice_names: tuple[str, ...] = ()  # non-empty only for voice-anchored plans


# --- Helpers ------------------------------------------------------------

def _slug(s: str) -> str:
    out: list[str] = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def _to_int_or_none(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _s(v: object) -> str:
    return "" if v is None else str(v).strip()


def _is_blank(row: tuple) -> bool:
    return not row or not any(c is not None and _s(c) for c in row)


# --- Loaders ------------------------------------------------------------

def load_keywords() -> list[KeywordRow]:
    wb = load_workbook(config.KEYWORDS_XLSX, read_only=True, data_only=True)
    out: list[KeywordRow] = []
    for tab, geo in _TAB_TO_GEOGRAPHY.items():
        ws = wb[tab]
        rows = list(ws.iter_rows(values_only=True))
        for r in rows[1:]:  # skip header
            if _is_blank(r):
                continue
            bucket, sub, kw = _s(r[0]), _s(r[1]), _s(r[2])
            if not kw:
                continue
            out.append(KeywordRow(geo, bucket, sub, kw))
    return out


def load_voices() -> list[Voice]:
    wb = load_workbook(config.VOICES_XLSX, read_only=True, data_only=True)
    out: list[Voice] = []
    for tab, geo in (("India", "India"), ("US", "US")):
        ws = wb[tab]
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        # US tab has a leading "#" col we skip
        offset = 1 if (header and _s(header[0]) == "#") else 0
        for r in it:
            if _is_blank(r):
                continue
            cells = list(r[offset:])
            while len(cells) < 10:
                cells.append(None)
            out.append(Voice(
                tier=_to_int_or_none(cells[0]),
                name=_s(cells[1]),
                category=_s(cells[2]),
                sub_domain=_s(cells[3]),
                role=_s(cells[4]),
                why=_s(cells[5]),
                channels=_s(cells[6]),
                confidence=_s(cells[7]),
                url_status=_s(cells[8]),
                linkedin_url=_s(cells[9]),
                geography=geo,
            ))
    return out


def load_newsletters() -> list[Newsletter]:
    # Layout: row 0 = banner, row 1 = blank spacer, row 2 = header, row 3+ = data.
    wb = load_workbook(config.VOICES_XLSX, read_only=True, data_only=True)
    ws = wb["Newsletters & Publications"]
    rows = list(ws.iter_rows(values_only=True))
    out: list[Newsletter] = []
    for r in rows[3:]:
        if _is_blank(r):
            continue
        cells = list(r) + [None] * max(0, 9 - len(r))
        out.append(Newsletter(
            tier=_to_int_or_none(cells[1]),
            name=_s(cells[2]),
            geography=_s(cells[3]),
            type_=_s(cells[4]),
            author=_s(cells[5]),
            description=_s(cells[6]),
            reach=_s(cells[7]),
            url=_s(cells[8]),
        ))
    return out


def load_company_pages() -> list[CompanyPage]:
    # Layout: row 0 = banner, row 1 = blank spacer, row 2 = header, row 3+ = data.
    wb = load_workbook(config.VOICES_XLSX, read_only=True, data_only=True)
    ws = wb["Company Pages"]
    rows = list(ws.iter_rows(values_only=True))
    out: list[CompanyPage] = []
    for r in rows[3:]:
        if _is_blank(r):
            continue
        cells = list(r) + [None] * max(0, 7 - len(r))
        out.append(CompanyPage(
            geography=_s(cells[1]),
            name=_s(cells[2]),
            type_=_s(cells[3]),
            description=_s(cells[4]),
            why_follow=_s(cells[5]),
            linkedin_url=_s(cells[6]),
        ))
    return out


# --- Plan builder -------------------------------------------------------

_PROMPT_TEMPLATE = (
    "Healthcare news from the last 24 hours from {geo_label}, focused on {bucket}.\n"
    "Sub-themes to consider: {sub_buckets}.\n"
    "Representative keywords: {keyword_sample}.\n"
    "For each story include: headline, source URL, publication date, 2-sentence summary.\n"
    "Prioritize: funding rounds, regulatory actions, product launches, leadership moves, "
    "M&A, and substantive policy news. Skip listicles and opinion pieces unless from a tier-1 voice."
)

_VOICE_PROMPT_TEMPLATE = (
    "Substantive posts, articles, podcasts, or interviews published in the last 24 hours by "
    "these tier-1 healthcare voices in {geo_label}: {voices}.\n"
    "Look across LinkedIn, X, Substacks, podcasts, op-eds, and media columns where applicable.\n"
    "For each item include: voice name, headline or topic, source URL, publication date, "
    "and a 2-sentence summary.\n"
    "Skip routine reposts, brief congratulatory replies, and short reactions. Focus on original "
    "takes, announcements, and substantive analysis."
)


def build_query_plans() -> list[QueryPlan]:
    rows = load_keywords()

    # Group keywords by (geography, bucket), preserving Excel order.
    grouped: dict[tuple[Geography, str], list[KeywordRow]] = {}
    bucket_first_idx: dict[tuple[Geography, str], int] = {}
    for i, kr in enumerate(rows):
        key = (kr.geography, kr.bucket)
        grouped.setdefault(key, []).append(kr)
        bucket_first_idx.setdefault(key, i)

    sorted_keys = sorted(
        grouped.keys(),
        key=lambda k: (_GEO_ORDER.index(k[0]), bucket_first_idx[k]),
    )

    plans: list[QueryPlan] = []
    for geo, bucket in sorted_keys:
        krows = grouped[(geo, bucket)]
        # Sub-buckets in first-seen order; keywords per sub-bucket also in Excel order.
        sub_seen: dict[str, list[KeywordRow]] = {}
        for kr in krows:
            sub_seen.setdefault(kr.sub_bucket, []).append(kr)
        sub_buckets = tuple(sub_seen.keys())

        sample: list[str] = []
        for members in sub_seen.values():
            for m in members[:KEYWORDS_PER_SUB_BUCKET_IN_PROMPT]:
                sample.append(m.keyword)

        prompt = _PROMPT_TEMPLATE.format(
            geo_label=_GEO_LABEL[geo],
            bucket=bucket,
            sub_buckets=", ".join(sub_buckets),
            keyword_sample=", ".join(sample),
        )

        plans.append(QueryPlan(
            id=f"{geo.lower()}__{_slug(bucket)}",
            geography=geo,
            bucket=bucket,
            sub_buckets=sub_buckets,
            keyword_sample=tuple(sample),
            keyword_count_total=len(krows),
            prompt_text=prompt,
        ))

    plans.extend(_build_voice_plans())
    return plans


def _build_voice_plans() -> list[QueryPlan]:
    """One voice-anchored Perplexity query per geography, naming tier-1 voices.

    Tier-1 voices mostly publish on LinkedIn / X — RSS won't reach them. These
    queries close that coverage gap. Tier 2-4 are too numerous to name in a
    single prompt without diluting signal; they ride along with bucket sweeps.
    """
    voices = load_voices()
    out: list[QueryPlan] = []
    for geo in ("India", "US"):
        tier1 = [v for v in voices if v.tier == 1 and v.geography == geo]
        if not tier1:
            continue
        names = tuple(v.name for v in tier1)
        prompt = _VOICE_PROMPT_TEMPLATE.format(
            geo_label=_GEO_LABEL[geo],
            voices=", ".join(names),
        )
        out.append(QueryPlan(
            id=f"{geo.lower()}__tier1_voices",
            geography=geo,
            bucket="Tier-1 voices",
            sub_buckets=(),
            keyword_sample=(),
            keyword_count_total=0,
            prompt_text=prompt,
            voice_names=names,
        ))
    return out


if __name__ == "__main__":
    plans = build_query_plans()
    print(f"Built {len(plans)} query plans.\n")
    for p in plans:
        print(
            f"  [{p.geography:>6}] {p.id:<55} "
            f"subs={len(p.sub_buckets):>2}  kws={p.keyword_count_total:>4}  "
            f"sample={len(p.keyword_sample):>3}"
        )
    print("\n--- Example prompt ---")
    print(plans[0].prompt_text)
