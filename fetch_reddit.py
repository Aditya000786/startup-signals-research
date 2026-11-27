"""Stage 1b: pull Reddit comments that describe a problem, via the free Arctic Shift archive.

Reddit's own API is closed to new apps and reddit.com blocks most scripts, so this
reads the public Arctic Shift archive (https://arctic-shift.photon-reddit.com). It
plays the role Algolia plays for fetch.py.

Arctic Shift's full-text search is slow (about 10 s per page, often "Timeout") and
matches words, not phrases. Plain paging through a subreddit is fast (about 1 s per
100 comments), so this script reads EVERY comment in each subreddit since --since and
keeps those that contain one of PHRASES exactly. Several subreddits run in parallel.

Raw output (the evidence behind every Reddit idea; commit it as
data/reddit/comments.jsonl.gz with `gzip -kf data/reddit/comments.jsonl`):
  data/reddit/comments.jsonl   kept comments, in fetch.py's format plus
                               source="reddit", url, score, sub
  data/reddit/progress.json    per subreddit: cursor, comments scanned, kept, done
Ids are "rd_<id>" and authors "reddit:<name>", so nothing clashes with HN rows.

Resumable at any point: each subreddit restarts from its saved cursor.
Afterwards, `python ralph.py extend` merges the new rows into data/comments.jsonl
and queues them for extraction.

  python fetch_reddit.py                          # all SUBREDDITS, last 2 years
  python fetch_reddit.py --subs SaaS freelance --since 2025-01-01 --workers 4
  python fetch_reddit.py --subs Teachers --shards 6   # one slow subreddit, 6 date slices
  python fetch_reddit.py --status
"""
import argparse, json, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://arctic-shift.photon-reddit.com/api"
API = BASE + "/comments/search"
FIELDS = "id,body,author,created_utc,link_id,subreddit,score"  # 5x smaller than full rows
DIR = Path(__file__).parent / "data" / "reddit"
OUT, PROG = DIR / "comments.jsonl", DIR / "progress.json"
UA = "startup-signals-research/0.2 (personal research script)"

# Where people describe their work. Trade subreddits usually beat founder subreddits
# for original product ideas: founders talk about founding, practitioners about pain.
# Ordered small -> large so useful data lands early.
SUBREDDITS = [
    "freelance", "restaurateur", "SomebodyMakeThis", "LawFirm", "projectmanagement",
    "PropertyManagement", "Bookkeeping", "AppIdeas", "humanresources", "marketing", "ecommerce",
    "Contractor", "shopify", "msp", "Etsy", "SaaS", "startups", "photography", "Dentistry",
    "devops", "Entrepreneur", "Construction", "accounting", "smallbusiness", "sysadmin",
    "realtors", "nursing", "Teachers",
]

# Exact phrases (lowercase, straight apostrophes). The HN list from fetch.py plus the
# ways Reddit users phrase the same thing.
sys.path.insert(0, str(Path(__file__).parent))
from fetch import PHRASES as HN_PHRASES  # noqa: E402

PHRASES = sorted({p.lower() for p in HN_PHRASES} | {
    "is there an app", "is there a tool", "is there any software", "is there a software",
    "is there a program that", "is there a website that", "wish someone would make",
    "wish someone would build", "willing to pay for a", "i'd gladly pay", "i would gladly pay",
    "no good tool", "no good software", "no good app", "there's no app", "doesn't exist yet",
    "still doing it manually", "do it all manually", "so much time manually", "hours a week manually",
    "still using spreadsheets", "spreadsheet hell", "tracking it in a spreadsheet",
    "a giant spreadsheet", "keep track in a spreadsheet", "such a pain to", "an absolute nightmare to",
    "every software we tried", "none of the software", "none of the tools", "we tried every",
    "the software we use is terrible", "our software sucks", "hate our software",
})
PHRASE_RE = re.compile("|".join(re.escape(p) for p in sorted(PHRASES, key=len, reverse=True)))

SKIP_AUTHORS = {"AutoModerator", "[deleted]", None}
lock = threading.Lock()


def norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("’", "'").lower())


def get(params, retries=8, url=API):
    url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:  # some requests hang; retry fast
                return json.load(r)["data"] or []
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            if e.code == 400:
                raise RuntimeError(f"bad request {params}: {body}")
            wait = int(e.headers.get("retry-after") or 0) or min(120, 10 * 2 ** attempt)
            print(f"  r/{params.get('subreddit', '-')}: HTTP {e.code} {body[:60]!r}, waiting {wait}s", flush=True)
            time.sleep(wait)
        except Exception:  # network hiccup / truncated read
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"Arctic Shift failed for {params}")


def load_progress():
    return json.loads(PROG.read_text()) if PROG.exists() else {}


def save_progress(prog):
    tmp = PROG.with_suffix(".tmp")
    tmp.write_text(json.dumps(prog, indent=1, sort_keys=True))
    tmp.replace(PROG)


def keep_row(c):
    text = (c.get("body") or "").strip()
    if c.get("author") in SKIP_AUTHORS or len(text) < 80 or text in ("[deleted]", "[removed]"):
        return None
    m = PHRASE_RE.search(norm(text))
    if not m:
        return None
    return {
        "id": "rd_" + c["id"], "author": "reddit:" + c["author"], "t": int(c["created_utc"]),
        "story_id": c.get("link_id"),
        "story_title": f"r/{c.get('subreddit')}:",  # post title filled in by add_titles()
        "text": text[:4000], "phrase": m.group(0), "source": "reddit",
        "sub": c.get("subreddit"), "score": c.get("score"),
        "url": f"https://www.reddit.com/r/{c.get('subreddit')}/comments/{(c.get('link_id') or '')[3:]}/_/{c['id']}/",
    }


def plan_shards(sub, lo, hi, prog, n):
    """Split a subreddit's remaining range into n slices, each with its own cursor.

    The slices are saved in progress.json under st["shards"], so a stopped run resumes
    every slice where it was; later runs reuse the saved slices whatever --shards says."""
    with lock:
        st = prog.setdefault(sub.lower(), {"cursor": lo, "scanned": 0, "kept": 0, "done": False, "since": lo})
        if st["done"] or st.get("shards") or n < 2:
            return st
        start = max(st["cursor"], lo)
        step = (hi - start) // n
        edges = [start + i * step for i in range(n)] + [hi]
        # each later slice starts 1 s early so no comment on an edge second is skipped;
        # the overlap is deduped by `seen`
        st["shards"] = [{"lo": a - (i > 0), "hi": b, "cursor": a - (i > 0), "scanned": 0, "kept": 0,
                         "done": False} for i, (a, b) in enumerate(zip(edges, edges[1:]))]
        save_progress(prog)
        return st


def finish_shards(st, prog):
    with lock:
        sh = st.get("shards")
        if sh and all(x["done"] for x in sh):
            st["scanned"] += sum(x["scanned"] for x in sh)
            st["kept"] += sum(x["kept"] for x in sh)
            st["cursor"], st["done"] = max(x["hi"] for x in sh), True
            del st["shards"]
            save_progress(prog)


def scan_sub(sub, lo, hi, prog, seen, st=None):
    if st is None:
        with lock:
            st = prog.setdefault(sub.lower(), {"cursor": lo, "scanned": 0, "kept": 0, "done": False, "since": lo})
    else:  # one slice of a sharded subreddit: scan only its own range
        lo, hi = st["lo"], st["hi"]
    if st["done"]:
        return
    cursor, pages, t0 = max(st["cursor"], lo), 0, time.time()
    while cursor < hi:
        rows = get({"subreddit": sub, "after": cursor, "before": hi, "sort": "asc", "limit": 100,
                    "fields": FIELDS})
        kept = []
        for c in rows:
            r = keep_row(c)
            if r and r["id"] not in seen:
                kept.append(r)
        with lock:
            for r in kept:
                seen.add(r["id"])
            if kept:
                with OUT.open("a") as f:
                    f.writelines(json.dumps(r) + "\n" for r in kept)
            st["scanned"] += len(rows); st["kept"] += len(kept)
            if len(rows) < 100:
                st["done"], st["cursor"] = True, hi
            else:
                last = int(rows[-1]["created_utc"])
                # same-second comments at a page edge are re-read; `seen` dedupes them
                st["cursor"] = last if last > cursor else cursor + 1
            cursor = st["cursor"]
            pages += 1
            if pages % 20 == 0 or st["done"]:
                save_progress(prog)
        if pages % 200 == 0:
            day = datetime.fromtimestamp(cursor, timezone.utc).date()
            print(f"  r/{sub}: at {day}, scanned {st['scanned']:,}, kept {st['kept']:,} "
                  f"({pages / (time.time() - t0):.1f} pages/s)", flush=True)
    print(f"r/{sub} done: scanned {st['scanned']:,}, kept {st['kept']:,}", flush=True)


def add_titles():
    """Fill in post titles for kept rows (the comment listing can't return them cheaply)."""
    if not OUT.exists():
        return
    rows = [json.loads(l) for l in OUT.open()]
    need = sorted({r["story_id"][3:] for r in rows if r["story_title"].endswith(":") and r.get("story_id")})
    titles = {}
    for i in range(0, len(need), 100):
        for p in get({"ids": ",".join(need[i:i + 100]), "fields": "id,title"}, url=BASE + "/posts/ids"):
            titles[p["id"]] = p.get("title") or ""
    for r in rows:
        t = titles.get((r.get("story_id") or "")[3:])
        if t is not None and r["story_title"].endswith(":"):
            r["story_title"] += " " + t
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(OUT)
    print(f"titles: looked up {len(need)} posts")


def status():
    prog = load_progress()
    tot_s = tot_k = 0
    for sub in SUBREDDITS:
        st = prog.get(sub.lower())
        if not st:
            print(f"{sub:20s} not started"); continue
        sh = st.get("shards") or []
        s_n = st["scanned"] + sum(x["scanned"] for x in sh)
        k_n = st["kept"] + sum(x["kept"] for x in sh)
        day = datetime.fromtimestamp(st["cursor"], timezone.utc).date()
        state = "done" if st["done"] else (f"{sum(x['done'] for x in sh)}/{len(sh)} slices" if sh else f"at {day}")
        print(f"{sub:20s} {state:14s} scanned {s_n:>10,}  kept {k_n:>7,}")
        for x in sh:
            d0, d1, dc = (datetime.fromtimestamp(x[k], timezone.utc).date() for k in ("lo", "hi", "cursor"))
            print(f"  {d0} .. {d1}  {'done' if x['done'] else f'at {dc}':14s} kept {x['kept']:>5,}")
        tot_s += s_n; tot_k += k_n
    print(f"{'TOTAL':20s} {'':14s} scanned {tot_s:>10,}  kept {tot_k:>7,}")


def main():
    ap = argparse.ArgumentParser()
    now = datetime.now(timezone.utc)
    ap.add_argument("--subs", nargs="*", default=SUBREDDITS)
    ap.add_argument("--since", default=f"{now.year - 2}-{now:%m}-01")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--shards", type=int, default=1,
                    help="split each unfinished subreddit's remaining range into N parallel slices")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return status()

    DIR.mkdir(parents=True, exist_ok=True)
    gz = OUT.with_suffix(".jsonl.gz")
    if not OUT.exists() and gz.exists():  # fresh clone: resume from the committed raw file
        import gzip, shutil
        with gzip.open(gz, "rb") as fi, OUT.open("wb") as fo:
            shutil.copyfileobj(fi, fo)
    seen = {json.loads(l)["id"] for l in OUT.open()} if OUT.exists() else set()
    prog = load_progress()
    lo = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp())
    hi = int(time.time())
    print(f"{len(a.subs)} subreddits since {a.since}, {len(PHRASES)} phrases, {a.workers} workers", flush=True)
    jobs, sharded = [], []
    for s in a.subs:
        st = plan_shards(s, lo, hi, prog, a.shards)
        if st.get("shards"):
            sharded.append(st)
            jobs += [(s, x) for x in st["shards"]]
        else:
            jobs.append((s, None))
    with ThreadPoolExecutor(a.workers) as ex:
        for f in [ex.submit(scan_sub, s, lo, hi, prog, seen, x) for s, x in jobs]:
            f.result()
    for st in sharded:
        finish_shards(st, prog)
    with lock:
        save_progress(prog)
    add_titles()
    status()


if __name__ == "__main__":
    main()
