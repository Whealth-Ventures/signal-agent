"""Deterministic healthcare-topicality gate.

The LLM ranker (sonar-reasoning-pro) is the primary topicality judge: its prompt
tells it to drop anything that isn't healthcare. But when that call fails or its
output can't be parsed, the ranker falls back to score-order — and the
relevance_score CANNOT stand in for topicality, because the firm's content corpus
is full of VC/funding/deeptech language, so a non-healthcare "₹2,000 Cr Deeptech
VC Fund" story scores just as high as a real healthcare story.

This module is the safety net for that degraded path only: a curated lexicon of
unambiguous healthcare stems. `is_healthcare(text)` returns True if the title +
summary contains at least one stem. It is intentionally generous (a broad stem
list) to minimise false-drops; it only needs to catch the obvious non-healthcare
leakers (fintech, edtech, quick-commerce, EVs, sector-agnostic VC funds).

Edit `_STEMS` to tune. Stems are matched at a word boundary as prefixes, so
"surg" matches surgery/surgical/surgeon, "diagnos" matches diagnostic/diagnosis.
"""
from __future__ import annotations

import re

# Unambiguous healthcare stems. Prefix-matched at a left word boundary, so most
# inflections are covered by the stem. Ambiguous homographs (e.g. "care" vs
# "career") are constrained with a right boundary where noted.
_STEMS: tuple[str, ...] = (
    # core
    "health", "healthcare", "medic", "medical", "medicine", "medication",
    "clinic", "clinical", "clinician", "hospital", "hospitaliz", "patient",
    "physician", "doctor", "nurse", "caregiv", "elder care", "senior care",
    "primary care", "patient care", "home care", "aged care", "critical care",
    "eye care", "health care",
    # pharma / biotech / devices
    "pharma", "pharmaceutic", "biotech", "biopharma", "life science",
    "life-science", "lifescience", "medtech", "medical device", "drugmaker",
    "vaccin", "biosimilar", "biologic", "genom", "genetic", "molecul",
    "therap", "therapeut", "drug", "api manufactur", "cdmo", "formulation",
    # clinical / disease / specialties
    "diagnos", "disease", "illness", "syndrome", "disorder", "oncolog",
    "cancer", "tumor", "tumour", "cardio", "neuro", "ortho", "derma",
    "nephro", "pulmo", "gastro", "endocrin", "hepat", "renal", "pediatr",
    "paediatr", "geriatr", "obstetr", "gynec", "gynaec", "psychiat",
    "mental health", "behavioral health", "behavioural health", "dental",
    "dentist", "ophthalm", "glaucoma", "optometr", "radiolog", "patholog",
    "immunolog", "immuno", "epidemi", "pandemic", "infect", "surg",
    "ablation", "implant", "prosthet", "diabet", "obesity", "cardiac",
    "stroke", "alzheimer", "parkinson",
    # care delivery / digital health
    "telehealth", "telemedicine", "digital health", "health tech", "healthtech",
    "ehr", "emr", "wellness", "nutrition", "maternity", "fertility", "ivf",
    "reproductive health", "contracept", "biomark", "ambulance",
    "emergency medical", "diagnostic lab", "pathology lab", "screening",
    "public health", "preventive health", "chronic", "wearable health",
    # payors / policy (healthcare-specific only)
    "health insurance", "medicaid", "medicare", "ayushman", "pmjay", "abdm",
    "irdai", "cghs", "esic", "value-based care", "payer", "payor", "insurtech",
    "fda", "cdsco", "ema approval", "drug approval", "marketing authorization",
    # US health payors / programs that often appear without a generic health word
    "cms", "aca marketplace", "affordable care", "health plan", "health system",
    "managed care", "cigna", "aetna", "humana", "unitedhealth", "united health",
    "elevance", "centene", "molina", "kaiser permanente",
)

# Multi-word stems need a left boundary only; single tokens that have common
# non-health homographs get a right boundary too.
_RIGHT_BOUNDED = {"drug", "patient", "cancer", "stroke", "renal", "implant"}


def _compile() -> re.Pattern[str]:
    parts = []
    for stem in _STEMS:
        esc = re.escape(stem)
        if stem in _RIGHT_BOUNDED:
            parts.append(rf"\b{esc}s?\b")
        else:
            parts.append(rf"\b{esc}")
    return re.compile("|".join(parts), re.IGNORECASE)


_PATTERN = _compile()


def is_healthcare(text: str | None) -> bool:
    """True if the text contains at least one healthcare stem."""
    if not text:
        return False
    return _PATTERN.search(text) is not None
