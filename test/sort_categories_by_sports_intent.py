"""
sort_categories_by_sports_intent.py

Takes the FULL categoryFilterWords enum list (all ~4000+ values Apify's
compass/crawler-google-places actor accepts -- most of which have nothing
to do with sports) and ranks EVERY one of them by semantic similarity to
a broad set of sports-related anchor phrases, using a local
sentence-transformer embedding model.

WHY THIS EXISTS
---------------
categoryFilterWords requires exact lowercase string matches against
Google's own place-category taxonomy. That taxonomy classifies by
role/facility type ("golf instructor", "indoor golf course", "hockey
rink"), not by umbrella search terms ("sports academy"). A small,
hand-picked filter list will always miss category names you didn't
think to include -- confirmed empirically: a 6-word filter list dropped
84/90 real golf-related leads because none of the words matched
"golf instructor", "indoor golf course", etc.

This script is a ONE-TIME (or occasional, whenever the category list is
re-pulled) offline sort -- NOT part of the live pipeline. Output is a CSV
you look up manually before configuring a categoryFilterWords run for a
specific sport/search.

DESIGN NOTES
------------
- Anchors are broad and multi-angled (facility, instruction, retail,
  club/org, generic single-word forms, and a representative sample of
  bare single-word sport names) so both phrase categories ("golf
  instructor") and bare single-word categories ("golf", "rugby") score
  fairly. The single-word sport anchors are a representative sample, not
  an exhaustive enumeration -- the model still generalizes to sports not
  explicitly listed via embedding proximity, same as it does for phrases.
- "sports_related" is a SUGGESTED split using a fixed similarity
  threshold (default 0.6, override with --threshold). An earlier version
  used a data-driven "largest gap" elbow search instead -- on the real
  4036-category run, that latched onto the single biggest cliff in the
  curve (0.95 -> 0.90), which flagged only 16 categories as True while
  clearly-relevant ones like "golf instructor" (0.78), "tennis club"
  (0.75), and "martial arts school" (0.66) were left False. A fixed
  threshold matched manual review of the real output far better than a
  gap search on this particular long-tail distribution. Every row's real
  similarity_score is always in the CSV regardless of the flag, so the
  cutoff can be re-sliced directly in the CSV, or by re-running with a
  different --threshold (no re-embedding needed either way, if you save
  scores from a prior run -- though this script currently re-embeds each
  run for simplicity).
- Input parsing: the whole file is read and joined, then split on commas
  (not read as one-keyword-per-line), per how the source file is
  actually formatted. Each item is stripped of whitespace and surrounding
  quotes. Empty items are dropped. Duplicates are dropped, keeping first
  occurrence, so the sort isn't skewed by accidental repeats in the
  source list.

USAGE
-----
    pip install sentence-transformers --break-system-packages
    python sort_categories_by_sports_intent.py --input keywords.txt --output sorted_categories.csv

Optional:
    --model <name>   sentence-transformer model (default: all-MiniLM-L6-v2,
                      small/fast/CPU-friendly -- fine for an 8GB machine)
"""

import argparse
import csv
import re
import sys
from pathlib import Path


# =====================================================================
# Anchor phrases -- broad and multi-angled on purpose. Every plausible
# "shape" a sports-related Google category could take is represented, so
# the model has something close in form to compare against regardless of
# whether the real category is a single word, a facility name, a role
# name, a retail name, or a club/org name. NOT an enumeration of sports --
# adding "hockey" or "golf" here would defeat the point of using
# embeddings in the first place. These are intentionally generic.
# =====================================================================
SPORTS_ANCHORS: list[str] = [
    # Generic single-word forms -- critical for catching bare category
    # names like "tennis", "golf", "boxing" that a phrase-only anchor set
    # would under-score simply due to length/shape mismatch.
    "sport", "sports", "athletics", "fitness", "recreation",

    # Bare single-word sport names -- added after real output showed
    # single-word categories (e.g. "rugby" at only 0.679) scoring lower
    # than equivalent phrase categories, because the anchor set had no
    # same-shape (single bare word) match for them to land close to.
    # This is still not an exhaustive sport enumeration -- it's a
    # representative sample of shapes (ball sports, combat sports, water
    # sports, racket sports, track sports, etc.) so the model generalizes
    # to sports not explicitly listed here via embedding proximity, the
    # same way it already does for phrase categories.
    "tennis", "golf", "boxing", "hockey", "soccer", "basketball",
    "baseball", "football", "rugby", "cricket", "volleyball", "swimming",
    "wrestling", "badminton", "squash", "cycling", "running", "skiing",
    "skating", "climbing", "gymnastics", "martial arts", "yoga",

    # Facility / venue shape
    "sports facility", "sports complex", "sports club", "sports center",
    "athletic facility", "training facility", "training center",
    "recreation center", "fitness center", "gym", "arena", "stadium",
    "court", "field", "rink", "pool", "track", "range",

    # Instruction / coaching shape
    "sports instructor", "athletic coach", "personal trainer",
    "sports academy", "sports school", "coaching center",
    "youth sports program", "sports camp",

    # Retail / commercial shape
    "sports equipment store", "sporting goods store", "sports apparel",
    "athletic gear shop",

    # Club / organization shape
    "sports club", "athletic club", "sports league", "sports association",
    "sports organization", "recreational league",

    # Competitive / performance shape
    "competitive sport", "sports team", "sports training", "physical training",
]


def parse_keywords(raw_text: str) -> list[str]:
    """Join the whole file and split on commas (source format: all on one
    line or a few lines, comma-separated -- NOT one-per-line). Strips
    whitespace and surrounding quote characters per item. Drops empties.
    De-dupes while preserving first-seen order, case-insensitively, since
    categoryFilterWords itself is case-sensitive-lowercase but duplicate
    entries differing only in case would still be the same real category.

    Lines are joined with a comma, not a space -- if the source file wraps
    across multiple lines without a trailing comma on each line, joining
    with a bare space would silently glue the last word of one line to
    the first word of the next (e.g. "golf" + "sports bar" becoming the
    single bogus item "golf sports bar"). Joining with a comma instead
    means a missing trailing comma just produces one extra (harmless,
    stripped-to-empty-or-fine) split point rather than corrupting two
    real category names into one."""
    joined = ",".join(raw_text.splitlines())
    raw_items = joined.split(",")

    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        cleaned = item.strip().strip('"').strip("'").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


DEFAULT_SPORTS_THRESHOLD = 0.6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Path to text file containing the ~4000 keywords, comma-separated")
    parser.add_argument("--output", default="sorted_categories.csv", help="Output CSV path (default: sorted_categories.csv)")
    parser.add_argument("--model", default="all-MiniLM-L6-v2", help="Sentence-transformer model name (default: all-MiniLM-L6-v2)")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_SPORTS_THRESHOLD,
        help=f"Minimum similarity_score for sports_related=True (default: {DEFAULT_SPORTS_THRESHOLD}, "
             f"validated against real output -- everything above this on the actual 4036-category "
             f"run was genuinely sports/fitness-related; noise took over below ~0.2-0.3)."
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    raw_text = input_path.read_text(encoding="utf-8")
    keywords = parse_keywords(raw_text)

    if not keywords:
        print("ERROR: no keywords parsed from input file -- check the file format.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(keywords)} unique keyword(s) from {input_path}")

    try:
        from sentence_transformers import SentenceTransformer, util
    except ImportError:
        print(
            "ERROR: sentence-transformers not installed.\n"
            "Run: pip install sentence-transformers --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loading model '{args.model}' (first run downloads it, ~80MB for the default model)...")
    model = SentenceTransformer(args.model)

    print(f"Embedding {len(SPORTS_ANCHORS)} anchor phrase(s)...")
    anchor_embeddings = model.encode(SPORTS_ANCHORS, convert_to_tensor=True, normalize_embeddings=True)

    print(f"Embedding {len(keywords)} keyword(s)... (this is the slow step, runs once)")
    keyword_embeddings = model.encode(
        keywords, convert_to_tensor=True, normalize_embeddings=True,
        show_progress_bar=True, batch_size=64,
    )

    # Cosine similarity of every keyword against every anchor -- since
    # embeddings are normalized, dot product == cosine similarity.
    sim_matrix = util.cos_sim(keyword_embeddings, anchor_embeddings)  # shape: (n_keywords, n_anchors)

    results: list[dict] = []
    for i, kw in enumerate(keywords):
        row_scores = sim_matrix[i]
        best_anchor_idx = int(row_scores.argmax())
        best_score = float(row_scores[best_anchor_idx])
        results.append({
            "category": kw,
            "nearest_anchor": SPORTS_ANCHORS[best_anchor_idx],
            "similarity_score": round(best_score, 4),
        })

    results.sort(key=lambda r: r["similarity_score"], reverse=True)

    for r in results:
        r["sports_related"] = r["similarity_score"] >= args.threshold

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "nearest_anchor", "similarity_score", "sports_related"])
        writer.writeheader()
        writer.writerows(results)

    flagged_count = sum(1 for r in results if r["sports_related"])
    print(f"\n✅ Wrote {len(results)} row(s) to {args.output}")
    print(f"   Threshold used                : {args.threshold}")
    print(f"   Flagged sports_related=True   : {flagged_count}")
    print(f"   Flagged sports_related=False  : {len(results) - flagged_count}")
    print(f"   (Every row's real similarity_score is in the CSV regardless of the flag,")
    print(f"   so you can re-slice the cutoff yourself with --threshold on a re-run, or")
    print(f"   just by filtering the CSV column directly -- no need to re-embed.)")

    print("\nTop 15 by similarity_score:")
    for r in results[:15]:
        flag = "✅" if r["sports_related"] else "  "
        print(f"  {flag} {r['similarity_score']:.4f}  {r['category']:35s} (nearest: {r['nearest_anchor']})")


if __name__ == "__main__":
    main()