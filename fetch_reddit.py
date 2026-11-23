"""Stage 1b: pull Reddit comments that describe a problem, using the free Arctic Shift archive.

Reddit's own API is closed to new apps, and reddit.com blocks most scripts, so this
reads the public Arctic Shift archive (https://arctic-shift.photon-reddit.com) instead.
It plays the same role Algolia plays for fetch.py.

Arctic Shift's full-text search matches single words, not exact phrases (a multi-word
query matches the words loosely, and on big subreddits it often times out). So for
each subreddit this script searches a small set of ANCHOR words, pages through every
comment that contains one, and keeps only those that contain one of PHRASES exactly.

Output rows use fetch.py's format, so ralph.py / extract.py / clustering / the site need
no other changes. They are appended to data/comments.jsonl with:
  id      "rd_<reddit id>"        (no clash with HN's numeric ids)
  author  "reddit:<username>"     (keeps distinct-commenter counts honest across sources)
  story_title  "r/<sub>: <post title>"
  source  "reddit",  url  link to the comment

Safe to re-run: (subreddit, anchor) pairs already fetched are skipped
(data/reddit_fetched.json). Then run `python ralph.py extend` to queue the new comments, and
`gzip -kf data/comments.jsonl` so the committed raw file includes them.

  python fetch_reddit.py                       # default subreddits, last 3 years
  python fetch_reddit.py --subs SaaS freelance --since 2025-01-01
"""
import argparse, json, re, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://arctic-shift.photon-reddit.com/api/comments/search"
DATA = Path(__file__).parent / "data"
OUT = DATA / "comments.jsonl"
DONE = DATA / "reddit_fetched.json"
UA = "startup-signals-research/0.1 (personal research script)"

# Where people describe their work problems. Trade subreddits usually beat the
# founder subreddits for original ideas: founders talk about founding, not about pain.
SUBREDDITS = [
    # builders / owners
    "smallbusiness", "Entrepreneur", "SaaS", "startups", "ecommerce", "shopify", "freelance",
    # idea requests
    "SomebodyMakeThis", "AppIdeas",
    # trades and professions
    "sysadmin", "devops", "accounting", "Bookkeeping", "realtors", "PropertyManagement",
    "Teachers", "nursing", "Construction", "Contractor", "restaurateur", "msp", "marketing",
    "projectmanagement", "humanresources", "LawFirm", "Dentistry", "photography", "Etsy",
]

# anchor word searched on the server -> exact phrases kept locally (lowercase).
PHRASES = {
    "wish": ["i wish there was", "i wish there were", "wish someone would make", "wish someone would build"],
    # "pay" itself is in a large share of business comments (slow to scan, and pay
    # phrases already dominate the HN data), so pay intent is found via rarer words.
    "happily": ["i would happily pay", "i'd happily pay", "would happily pay for"],
    "gladly": ["i would gladly pay", "i'd gladly pay"],
    "willing": ["willing to pay for a", "willing to pay for something", "willing to pay good money"],
    "tool": ["is there a tool", "does anyone know of a tool", "would love a tool", "no good tool"],
    "app": ["is there an app", "is there a app", "why is there no app", "there's no app"],
    "software": ["is there a software", "is there any software", "no good software", "software that does"],
    "someone": ["someone should build", "someone should make", "somebody should build", "if someone built"],
    "script": ["i ended up writing a script", "i built a script", "i wrote a script to"],
    "built": ["we ended up building our own", "we built an internal tool", "ended up building our own"],
    "pain": ["biggest pain point", "a huge pain", "such a pain to", "the worst part of my job"],
    "nightmare": ["is a nightmare", "an absolute nightmare to"],
    "hours": ["spend hours every week", "waste hours", "hours every week", "hours a week manually"],
    "manually": ["so much manual work", "still doing it manually", "do it all manually", "manually copy"],
    "spreadsheet": ["still using spreadsheets", "spreadsheet hell", "tracking it in a spreadsheet",
                    "keep track in a spreadsheet", "a giant spreadsheet"],
}

SKIP_AUTHORS = {"AutoModerator", "[deleted]", None}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'").lower())


def get(params, retries=5):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)["data"] or []
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 422 and "Timeout" in body:
                # Arctic Shift says "Timeout. Maybe slow down a bit": usually load,
                # not query size. Back off and retry; split the window only if it persists.
                if attempt >= 2:
                    return None
                time.sleep(15 * (attempt + 1)); continue
            if e.code == 429:
                time.sleep(int(e.headers.get("retry-after") or 30)); continue
            if e.code == 400:
                raise RuntimeError(f"bad request {params}: {body[:200]}")
        except Exception as e:  # network hiccup
            print(f"  retry ({e})")
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Arctic Shift failed for {params}")


def scan(sub, anchor, lo, hi, keep, depth=0):
    """Page through every comment in r/sub containing `anchor` with lo <= created < hi."""
    cursor = lo
    while cursor < hi:
        rows = get({"subreddit": sub, "body": anchor, "after": cursor, "before": hi,
                    "sort": "asc", "limit": 100})
        if rows is None:  # timed out: split the remaining window in half
            if hi - cursor < 3600 or depth > 12:
                print(f"  giving up on r/{sub} '{anchor}' {cursor}-{hi}"); return
            mid = (cursor + hi) // 2
            scan(sub, anchor, cursor, mid, keep, depth + 1)
            scan(sub, anchor, mid, hi, keep, depth + 1)
            return
        for c in rows:
            keep(c, anchor)
        if len(rows) < 100:
            return
        last = int(rows[-1]["created_utc"])
        cursor = last if last > cursor else cursor + 1
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs", nargs="*", default=SUBREDDITS)
    ap.add_argument("--anchors", nargs="*", default=list(PHRASES))
    ap.add_argument("--since", default=f"{datetime.now(timezone.utc).year - 3}-01-01")
    a = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    if not OUT.exists() and (DATA / "comments.jsonl.gz").exists():
        raise SystemExit("run `gunzip -k data/comments.jsonl.gz` first, so Reddit rows join the HN ones")
    seen = {json.loads(l)["id"] for l in OUT.open()} if OUT.exists() else set()
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    lo = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp())
    hi = int(time.time())

    for sub in a.subs:
        for anchor in a.anchors:
            key = f"{sub.lower()}|{anchor}|{a.since}"
            if key in done:
                continue
            new, scanned = [], [0]

            def keep(c, anchor):
                scanned[0] += 1
                cid, text = "rd_" + c["id"], (c.get("body") or "").strip()
                if cid in seen or c.get("author") in SKIP_AUTHORS or len(text) < 80:
                    return
                if text in ("[deleted]", "[removed]"):
                    return
                t = norm(text)
                hit = next((p for p in PHRASES[anchor] if p in t), None)
                if not hit:
                    return
                seen.add(cid)
                new.append({
                    "id": cid, "author": "reddit:" + c["author"], "t": int(c["created_utc"]),
                    "story_id": c.get("link_id"),
                    "story_title": f"r/{c.get('subreddit', sub)}: {c.get('link_title') or ''}".strip(),
                    "text": text[:4000], "phrase": hit, "source": "reddit",
                    "url": "https://www.reddit.com" + (c.get("permalink") or ""),
                })

            scan(sub, anchor, lo, hi, keep)
            with OUT.open("a") as f:
                for r in new:
                    f.write(json.dumps(r) + "\n")
            done.add(key)
            DONE.write_text(json.dumps(sorted(done)))
            print(f"r/{sub} '{anchor}': scanned {scanned[0]}, kept {len(new)} (total comments {len(seen)})")


if __name__ == "__main__":
    main()
