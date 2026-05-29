"""Aggregate Slack reactions into proposed tuning adjustments.

Reads the `feedback` table (populated by `feedback_puller`), joins to
`digests` + `digest_stories` + `stories`, classifies each scored digest as
upvoted / downvoted / neutral based on net reaction count, then compares the
composition of upvoted vs downvoted digests to propose booster-weight
adjustments.

v1 produces a single proposal type: `booster_weight_adjustment`. Each
proposal is fully self-describing (current value, proposed value, evidence)
so the admin UI can present it without server-side state.

Output: writes proposals/pending.json to the repo (same pattern as
tuning.xlsx and prompts/) plus an immutable copy under proposals/history/.
The admin UI reads pending.json via the existing GitHub plumbing. The
aggregator does NOT mutate tuning.xlsx directly — application happens via
the admin UI's accept-proposal flow.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
import storage
from tunables import load_tunables

# Per-emoji polarity. Keys are Slack reaction names (no colons).
_POSITIVE = {"+1", "thumbsup", "heart", "fire", "white_check_mark", "100"}
_NEGATIVE = {"-1", "thumbsdown", "x", "no_entry_sign"}

# Minimums to make any proposal at all — we don't want noise from 1-2 days.
MIN_UPVOTED_DIGESTS = 3
MIN_DOWNVOTED_DIGESTS = 3
MIN_BOOSTER_MATCHES_PER_GROUP = 5
DIVERGENCE_THRESHOLD = 0.20   # 20 percentage points
WEIGHT_STEP = 0.02            # how much to nudge a booster

_PROPOSALS_DIR = config.ROOT / "proposals"
_PENDING_PATH = _PROPOSALS_DIR / "pending.json"
_HISTORY_DIR = _PROPOSALS_DIR / "history"


# --- Data shapes -------------------------------------------------------

@dataclass
class DigestScore:
    digest_id: str
    digest_date: str
    slack_ts: str | None
    positive: int
    negative: int
    classification: str  # "upvoted" | "downvoted" | "neutral"

    @property
    def net(self) -> int:
        return self.positive - self.negative


@dataclass
class Proposal:
    id: str
    type: str
    target: dict
    current: float
    proposed: float
    rationale: str
    evidence: dict


@dataclass
class AggregationResult:
    generated_at: str
    window_days: int
    scored_digest_count: int
    upvoted_digest_count: int
    downvoted_digest_count: int
    proposals: list[Proposal] = field(default_factory=list)
    digest_scores: list[DigestScore] = field(default_factory=list)


# --- Pure logic --------------------------------------------------------

def _classify(positive: int, negative: int) -> str:
    if positive > negative:
        return "upvoted"
    if negative > positive:
        return "downvoted"
    return "neutral"


def _score_digests(conn: sqlite3.Connection, window_days: int) -> list[DigestScore]:
    """Compute reaction-net per digest in the window. Only digests with at
    least one linked reaction event are considered 'scored'."""
    cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
    rows = conn.execute(
        """
        SELECT d.id, d.digest_date, d.slack_ts,
               SUM(CASE WHEN f.event_type='reaction_added'
                         AND f.reaction IN ({pos}) THEN 1 ELSE 0 END) AS pos,
               SUM(CASE WHEN f.event_type='reaction_added'
                         AND f.reaction IN ({neg}) THEN 1 ELSE 0 END) AS neg
        FROM digests d
        JOIN feedback f ON f.digest_id = d.id
        WHERE d.sent_at IS NOT NULL
          AND strftime('%s', d.sent_at) >= ?
        GROUP BY d.id, d.digest_date, d.slack_ts
        HAVING pos > 0 OR neg > 0
        """.format(
            pos=",".join("?" * len(_POSITIVE)),
            neg=",".join("?" * len(_NEGATIVE)),
        ),
        (*_POSITIVE, *_NEGATIVE, str(int(cutoff))),
    ).fetchall()
    out: list[DigestScore] = []
    for r in rows:
        p, n = int(r["pos"] or 0), int(r["neg"] or 0)
        out.append(DigestScore(
            digest_id=r["id"], digest_date=r["digest_date"],
            slack_ts=r["slack_ts"],
            positive=p, negative=n,
            classification=_classify(p, n),
        ))
    return out


def _titles_per_digest(
    conn: sqlite3.Connection, digest_ids: list[str],
) -> dict[str, list[str]]:
    """Return {digest_id: [story_title, ...]} for joining against booster regexes."""
    if not digest_ids:
        return {}
    placeholders = ",".join("?" * len(digest_ids))
    rows = conn.execute(
        f"""
        SELECT ds.digest_id, s.canonical_title
        FROM digest_stories ds
        JOIN stories s ON s.id = ds.story_id
        WHERE ds.digest_id IN ({placeholders})
        """,
        digest_ids,
    ).fetchall()
    out: dict[str, list[str]] = {d: [] for d in digest_ids}
    for r in rows:
        out[r["digest_id"]].append(r["canonical_title"] or "")
    return out


def _booster_match_rate(
    titles_per_digest: dict[str, list[str]],
    digest_ids: list[str],
    pattern: re.Pattern[str] | None,
) -> tuple[int, int]:
    """For a list of digests, count stories matching the pattern vs total.
    Returns (matches, total). pattern=None matches nothing (boosters that
    don't have regex anchors — tier1_voice, etc. — can't be evaluated this way)."""
    if pattern is None:
        return 0, 0
    matches = 0
    total = 0
    for did in digest_ids:
        for title in titles_per_digest.get(did, []):
            total += 1
            if pattern.search(title):
                matches += 1
    return matches, total


def _make_proposals(
    digest_scores: list[DigestScore],
    titles_per_digest: dict[str, list[str]],
    boosters: dict[str, tuple[float, re.Pattern[str] | None]],
    now_iso: str,
) -> list[Proposal]:
    upvoted = [d.digest_id for d in digest_scores if d.classification == "upvoted"]
    downvoted = [d.digest_id for d in digest_scores if d.classification == "downvoted"]
    if len(upvoted) < MIN_UPVOTED_DIGESTS or len(downvoted) < MIN_DOWNVOTED_DIGESTS:
        return []

    proposals: list[Proposal] = []
    for name, (weight, pattern) in boosters.items():
        up_matches, up_total = _booster_match_rate(titles_per_digest, upvoted, pattern)
        dn_matches, dn_total = _booster_match_rate(titles_per_digest, downvoted, pattern)
        if up_matches < MIN_BOOSTER_MATCHES_PER_GROUP and dn_matches < MIN_BOOSTER_MATCHES_PER_GROUP:
            continue
        up_rate = up_matches / up_total if up_total else 0.0
        dn_rate = dn_matches / dn_total if dn_total else 0.0
        divergence = up_rate - dn_rate
        if abs(divergence) < DIVERGENCE_THRESHOLD:
            continue
        step = WEIGHT_STEP if divergence > 0 else -WEIGHT_STEP
        proposed = round(weight + step, 4)
        direction = "boost" if step > 0 else "demote"
        proposals.append(Proposal(
            id=f"p_{now_iso[:10]}_booster_{name}",
            type="booster_weight_adjustment",
            target={"sheet": "Boosters", "row_name": name, "column": "weight"},
            current=weight,
            proposed=proposed,
            rationale=(
                f"Booster '{name}' matched {up_rate:.0%} of stories in upvoted "
                f"digests vs {dn_rate:.0%} in downvoted "
                f"(n_up={len(upvoted)} digests / {up_matches} matches, "
                f"n_down={len(downvoted)} digests / {dn_matches} matches). "
                f"Propose to {direction} by {abs(step)}."
            ),
            evidence={
                "upvoted_digest_count": len(upvoted),
                "downvoted_digest_count": len(downvoted),
                "upvoted_match_rate": round(up_rate, 3),
                "downvoted_match_rate": round(dn_rate, 3),
                "upvoted_matches": up_matches,
                "downvoted_matches": dn_matches,
            },
        ))
    return proposals


# --- File output -------------------------------------------------------

def _write_proposals_files(result: "AggregationResult") -> tuple[Path, Path]:
    """Write pending.json + an immutable history copy. Returns the two paths."""
    body = {
        "generated_at": result.generated_at,
        "window_days": result.window_days,
        "scored_digest_count": result.scored_digest_count,
        "upvoted_digest_count": result.upvoted_digest_count,
        "downvoted_digest_count": result.downvoted_digest_count,
        "proposals": [asdict(p) for p in result.proposals],
        "digest_scores": [asdict(s) for s in result.digest_scores],
    }
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _PENDING_PATH.write_text(json.dumps(body, indent=2), encoding="utf-8")
    history_name = result.generated_at.replace(":", "-") + ".json"
    history_path = _HISTORY_DIR / history_name
    history_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return _PENDING_PATH, history_path


# --- Orchestrator ------------------------------------------------------

def aggregate(
    *,
    window_days: int = 30,
    conn: sqlite3.Connection | None = None,
    write_files: bool = True,
) -> AggregationResult:
    own_conn = conn is None
    c = conn or storage.connect()
    try:
        storage.init_db(conn=c)
        scores = _score_digests(c, window_days)
        digest_ids = [d.digest_id for d in scores]
        titles = _titles_per_digest(c, digest_ids)
        tunes = load_tunables()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        proposals = _make_proposals(scores, titles, tunes.boosters, now_iso)

        result = AggregationResult(
            generated_at=now_iso,
            window_days=window_days,
            scored_digest_count=len(scores),
            upvoted_digest_count=sum(1 for s in scores if s.classification == "upvoted"),
            downvoted_digest_count=sum(1 for s in scores if s.classification == "downvoted"),
            proposals=proposals,
            digest_scores=scores,
        )

        if write_files:
            _write_proposals_files(result)

        return result
    finally:
        if own_conn:
            c.close()


if __name__ == "__main__":
    r = aggregate()
    print(json.dumps({
        "scored_digest_count": r.scored_digest_count,
        "upvoted_digest_count": r.upvoted_digest_count,
        "downvoted_digest_count": r.downvoted_digest_count,
        "proposal_count": len(r.proposals),
        "generated_at": r.generated_at,
        "pending_file": str(_PENDING_PATH.relative_to(config.ROOT)),
    }, indent=2))
