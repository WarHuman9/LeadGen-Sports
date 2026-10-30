"""
Diagnose why categoryFilterWords is zeroing out results.

Usage:
    python diagnose_category_filter.py --dataset-id <UNFILTERED_RUN_DATASET_ID> --filter-words words.txt
    python diagnose_category_filter.py --dataset-id <UNFILTERED_RUN_DATASET_ID> --filter-words-inline "sports academy,football training,coaching center"

What it does:
1. Pulls the dataset from your unfiltered run (the one that got 90/110).
2. Extracts the raw `categoryName` + `categories` array Google actually
   returned for each place.
3. Compares those against your categoryFilterWords list using the exact
   same matching rule the actor claims to use (exact lowercase substring
   match against any category in the place's `categories` array).
4. Reports:
   - Which of YOUR filter words matched zero places (dead/renamed words)
   - Which categories Google returned that were NOT covered by your list
     (candidates to add)
   - A simulated "what would the filtered run have kept" count, so you
     can see if replaying your filter list against TODAY's category data
     reproduces the <10 result collapse.

This isolates whether the problem is:
  (a) your filter word list vs Google's current category taxonomy, or
  (b) the actor's internal matching logic changing.

If the simulated filtered count in step 4 comes out close to your real
<10 result run, the fault is almost certainly (a) -- taxonomy drift on
Google's side, not an Apify regression. If the simulated count matches
what you'd expect (~90), but your actual filtered API run still returned
<10, that would actually point toward (b) -- the actor's live matching
behavior misbehaving independent of the category data itself, which
would be worth filing to Apify with this same evidence attached.
"""

import argparse
import json
import os
import sys
from collections import Counter

try:
    from apify_client import ApifyClient
except ImportError:
    print("Installing apify-client...", file=sys.stderr)
    os.system(f"{sys.executable} -m pip install apify-client --break-system-packages -q")
    from apify_client import ApifyClient


def load_filter_words(args) -> list[str]:
    if args.filter_words:
        with open(args.filter_words, "r", encoding="utf-8") as f:
            words = [line.strip() for line in f if line.strip()]
    elif args.filter_words_inline:
        words = [w.strip() for w in args.filter_words_inline.split(",") if w.strip()]
    else:
        print("ERROR: provide --filter-words <file> or --filter-words-inline 'a,b,c'", file=sys.stderr)
        sys.exit(1)

    # Actor requires exact lowercase matches -- surface any that aren't
    # already lowercase, since that alone can silently zero out matches.
    non_lower = [w for w in words if w != w.lower()]
    if non_lower:
        print(f"⚠️  {len(non_lower)} filter word(s) are NOT lowercase (actor requires exact lowercase match):")
        for w in non_lower:
            print(f"    '{w}' -> should be '{w.lower()}'")
        print()

    return [w.lower() for w in words]


def fetch_places(client: ApifyClient, dataset_id: str) -> list[dict]:
    dataset = client.dataset(dataset_id)
    items = list(dataset.iterate_items())
    print(f"Fetched {len(items)} places from dataset {dataset_id}\n")
    return items


def get_place_categories(place: dict) -> list[str]:
    """
    Compass actor output includes both `categoryName` (primary) and
    `categories` (full array of all categories the place is listed under).
    We check both since categoryFilterWords likely matches against the
    full array, not just the primary one.
    """
    cats = []
    if place.get("categoryName"):
        cats.append(place["categoryName"])
    if place.get("categories"):
        cats.extend(place["categories"])
    # de-dupe, preserve order, lowercase for matching
    seen = set()
    out = []
    for c in cats:
        cl = c.strip().lower()
        if cl and cl not in seen:
            seen.add(cl)
            out.append(cl)
    return out


def matches_filter(place_categories: list[str], filter_words: list[str]) -> list[str]:
    """Return which filter words matched this place (exact match against any category)."""
    matched = []
    for fw in filter_words:
        if fw in place_categories:
            matched.append(fw)
    return matched


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-id", required=True, help="Dataset ID from your UNFILTERED run (the 90/110 one)")
    parser.add_argument("--filter-words", help="Path to a text file, one categoryFilterWord per line")
    parser.add_argument("--filter-words-inline", help="Comma-separated categoryFilterWords, e.g. 'sports academy,football training'")
    parser.add_argument("--apify-token", default=os.environ.get("APIFY_TOKEN"), help="Apify API token (or set APIFY_TOKEN env var)")
    args = parser.parse_args()

    if not args.apify_token:
        print("ERROR: provide --apify-token or set APIFY_TOKEN env var", file=sys.stderr)
        sys.exit(1)

    filter_words = load_filter_words(args)
    print(f"Loaded {len(filter_words)} filter words.\n")

    client = ApifyClient(args.apify_token)
    places = fetch_places(client, args.dataset_id)

    if not places:
        print("No places found in dataset -- check the dataset ID.")
        sys.exit(1)

    # Tally every category Google actually returned across all places
    all_categories_seen = Counter()
    filter_word_hit_count = Counter({fw: 0 for fw in filter_words})
    would_survive_filter = []
    would_be_excluded = []

    for place in places:
        cats = get_place_categories(place)
        all_categories_seen.update(cats)

        hits = matches_filter(cats, filter_words)
        for h in hits:
            filter_word_hit_count[h] += 1

        name = place.get("title") or place.get("name") or "(unnamed)"
        if hits:
            would_survive_filter.append((name, cats, hits))
        else:
            would_be_excluded.append((name, cats))

    print("=" * 70)
    print("SIMULATION: replaying your filter list against TODAY's category data")
    print("=" * 70)
    print(f"Would survive filter : {len(would_survive_filter)} / {len(places)}")
    print(f"Would be excluded    : {len(would_be_excluded)} / {len(places)}")
    print()

    print("-" * 70)
    print("Filter words that matched ZERO places (dead / renamed / mismatched):")
    print("-" * 70)
    dead_words = [fw for fw, count in filter_word_hit_count.items() if count == 0]
    if dead_words:
        for fw in dead_words:
            print(f"  ❌ '{fw}'")
    else:
        print("  (none -- every filter word matched at least one place)")
    print()

    print("-" * 70)
    print("Filter words that DID match, with hit counts:")
    print("-" * 70)
    for fw, count in sorted(filter_word_hit_count.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  ✅ '{fw}': {count} place(s)")
    print()

    print("-" * 70)
    print("Top 25 categories Google actually returned (across all places):")
    print("-" * 70)
    for cat, count in all_categories_seen.most_common(25):
        in_filter = "✅ in your filter list" if cat in filter_words else "   NOT in your filter list"
        print(f"  {count:3d}x  {cat:40s} {in_filter}")
    print()

    print("-" * 70)
    print("Sample of EXCLUDED places (would be dropped by your filter) -- check if these are ones you actually want:")
    print("-" * 70)
    for name, cats in would_be_excluded[:15]:
        print(f"  • {name}")
        print(f"      categories: {cats}")
    if len(would_be_excluded) > 15:
        print(f"  ... and {len(would_be_excluded) - 15} more")
    print()

    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    survive_pct = len(would_survive_filter) / len(places) * 100
    if survive_pct < 15:
        print(
            f"Simulated filter keeps only {survive_pct:.0f}% of places, closely matching your\n"
            f"real <10/110 result. This points to (a): your categoryFilterWords list not\n"
            f"matching TODAY's category taxonomy -- likely because Google renamed/split\n"
            f"categories for this niche/region, not an Apify actor regression.\n\n"
            f"Next step: review the 'NOT in your filter list' categories above and add\n"
            f"the relevant ones (in exact lowercase) to categoryFilterWords."
        )
    else:
        print(
            f"Simulated filter keeps {survive_pct:.0f}% of places, which does NOT match your\n"
            f"real <10/110 result. That mismatch suggests the actor's live filtering\n"
            f"behavior during the actual API run misbehaved independent of the category\n"
            f"data itself (b) -- worth filing an issue to Apify with this script's output\n"
            f"attached as evidence, since the taxonomy itself doesn't explain the drop."
        )


if __name__ == "__main__":
    main()