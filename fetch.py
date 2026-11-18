"""Stage 1: pull high-signal HN comments from the free Algolia HN Search API.

Searches exact "pain / wish / willingness-to-pay" phrases across every year of
HN, splits time windows whenever a query hits Algolia's 1000-result cap, and
writes deduplicated comments to data/comments.jsonl. Safe to re-run: phrases
already fetched are skipped (delete data/comments.jsonl to start over).
"""
import argparse, calendar, html, json, re, time, urllib.parse, urllib.request
from pathlib import Path

API = "https://hn.algolia.com/api/v1/search"
OUT = Path(__file__).parent / "data" / "comments.jsonl"
DONE = Path(__file__).parent / "data" / "fetched_phrases.json"

# Phrases that tend to precede a real, felt problem or demand signal.
PHRASES = [
    "I wish there was", "I wish there were", "I would pay for", "I'd pay for",
    "I would happily pay", "I'd happily pay", "take my money", "would pay good money",
    "why is there no", "why isn't there a", "someone should build", "someone should make",
    "somebody should build", "is there a tool that", "is there a service that",
    "does anyone know of a tool", "there's no good way to", "there is no good way to",
    "still no good solution", "nobody has solved", "nobody has figured out",
    "we ended up building our own", "we built an internal tool", "I ended up writing a script",
    "I built a script to", "biggest pain point", "the worst part of my job",
    "spend hours every week", "waste hours", "so much manual work",
    "a huge pain", "is a nightmare", "every company reinvents",
    "would love a tool", "I'd love a service", "if someone built",
    "I'd switch in a heartbeat", "the market is ripe", "ripe for disruption",
]

START_YEAR = 2007


def clean(text: str) -> str:
    text = re.sub(r"<p>", "\n\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def query(phrase, lo, hi, page=0, retries=4):
    params = {
        "query": f'"{phrase}"', "tags": "comment", "advancedSyntax": "true",
        "hitsPerPage": 1000, "page": page,
        "numericFilters": f"created_at_i>={lo},created_at_i<{hi}",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccup / rate limit: back off
            wait = 2 ** attempt
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"Algolia failed for {phrase!r} {lo}-{hi}")


def fetch_window(phrase, lo, hi, sink):
    d = query(phrase, lo, hi)
    if d["nbHits"] > 1000 and hi - lo > 86400 * 7:
        mid = (lo + hi) // 2  # too many hits for one window: split in half
        fetch_window(phrase, lo, mid, sink)
        fetch_window(phrase, mid, hi, sink)
        return
    for h in d["hits"]:
        text = clean(h.get("comment_text"))
        if len(text) < 80:  # one-liners rarely describe a problem
            continue
        sink.append({
            "id": h["objectID"], "author": h.get("author"),
            "t": int(h["created_at_i"]), "story_id": h.get("story_id"),
            "story_title": h.get("story_title"), "text": text[:4000],
            "phrase": phrase,
        })
    time.sleep(0.25)  # stay polite (Algolia allows ~10k req/hour)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=START_YEAR)
    ap.add_argument("--phrases", nargs="*", help="override the phrase list")
    args = ap.parse_args()

    OUT.parent.mkdir(exist_ok=True)
    seen = set()
    if OUT.exists():
        seen = {json.loads(l)["id"] for l in OUT.open()}
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()

    now = int(time.time())
    for phrase in args.phrases or PHRASES:
        if phrase in done:
            continue
        sink = []
        for year in range(args.from_year, time.gmtime().tm_year + 1):
            lo = calendar.timegm((year, 1, 1, 0, 0, 0))
            hi = min(calendar.timegm((year + 1, 1, 1, 0, 0, 0)), now)
            fetch_window(phrase, lo, hi, sink)
        new = [c for c in sink if c["id"] not in seen]
        with OUT.open("a") as f:
            for c in new:
                seen.add(c["id"])
                f.write(json.dumps(c) + "\n")
        done.add(phrase)
        DONE.write_text(json.dumps(sorted(done)))
        print(f"{phrase!r}: {len(sink)} hits, {len(new)} new (total {len(seen)})")


if __name__ == "__main__":
    main()
