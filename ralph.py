"""Ralph-loop helper for the Pro-only (no API) version of extract.py.

Claude Code itself reads the comments chunk by chunk. All state lives in data/:
  ralph_order.json   comment ids in processing order (strongest signals first)
  progress.json      {"next": <chunk index>, "chunk_size": N, "chunks": total}
  signals.jsonl      same format extract.py writes (is_signal rows only + a
                     marker row per chunk so reruns never double-count)

Usage:
  python ralph.py init [--chunk 150]   build the order + progress (once)
  python ralph.py next                 print the next unprocessed chunk
  python ralph.py save <chunk> <tsv>   record Claude's verdicts for that chunk
  python ralph.py status
  python ralph.py extend               queue comments added to comments.jsonl since
                                       init (e.g. by fetch_reddit.py) as NEW chunks at
                                       the end; finished chunks are untouched
  python ralph.py restratify           re-order the UNREAD comments round-robin by
                                       search phrase (read chunks keep their ids)
TSV columns (one line per signal; non-signals are simply omitted):
  id <TAB> problem <TAB> who <TAB> pay(none|implied|explicit) <TAB> intensity(1-5) <TAB> category
"""
import json, re, sys, time
from pathlib import Path

DATA = Path(__file__).parent / "data"
ORDER, PROG, SIG = DATA / "ralph_order.json", DATA / "progress.json", DATA / "signals.jsonl"
sys.path.insert(0, str(Path(__file__).parent))
from extract import CATEGORIES  # noqa: E402

STRONG = {"I would pay for": 3, "I'd pay for": 3, "I would happily pay": 4, "I'd happily pay": 4,
          "take my money": 3, "would pay good money": 4, "we ended up building our own": 4,
          "we built an internal tool": 4, "I ended up writing a script": 3, "I built a script to": 3,
          "biggest pain point": 3, "spend hours every week": 3, "waste hours": 2,
          "so much manual work": 3, "there's no good way to": 2, "there is no good way to": 2,
          "still no good solution": 3, "I'd switch in a heartbeat": 3}


def comments():
    return {c["id"]: c for c in map(json.loads, (DATA / "comments.jsonl").open())}


def init(chunk=150):
    com = comments()
    now = time.time()

    def prio(c):
        recency = max(0, 1 - (now - c["t"]) / (12 * 365 * 86400))  # 0 for >12y old
        return STRONG.get(c["phrase"], 1) + 2 * recency + min(len(c["text"]), 1500) / 1500

    ids = sorted(com, key=lambda i: -prio(com[i]))
    ORDER.write_text(json.dumps(ids))
    n = -(-len(ids) // chunk)
    if not PROG.exists():
        PROG.write_text(json.dumps({"next": 0, "chunk_size": chunk, "chunks": n, "done": []}))
    print(f"{len(ids)} comments -> {n} chunks of {chunk}")


def load():
    return json.loads(PROG.read_text()), json.loads(ORDER.read_text())


def next_chunk():
    p, ids = load()
    done = set(p["done"])
    k = next((i for i in range(p["chunks"]) if i not in done), None)
    if k is None:
        print("ALL DONE"); return
    com = comments()
    part = ids[k * p["chunk_size"] : (k + 1) * p["chunk_size"]]
    print(f"CHUNK {k} of {p['chunks']} ({len(done)} done)")
    for i in part:
        c = com[i]
        txt = re.sub(r"\s+", " ", c["text"])[:1100]
        print(f"[{i}|{time.gmtime(c['t']).tm_year}|{(c.get('story_title') or '')[:60]}] {txt}")


def save(k, tsv):
    p, ids = load()
    k = int(k)
    if k in p["done"]:
        print(f"chunk {k} already saved, skipping"); return
    part = set(ids[k * p["chunk_size"] : (k + 1) * p["chunk_size"]])
    rows, bad = [], 0
    for line in Path(tsv).read_text().splitlines():
        f = [x.strip() for x in line.split("\t")]
        if len(f) != 6 or f[0] not in part:
            bad += bool(line.strip()); continue
        cid, problem, who, pay, inten, cat = f
        if pay not in ("none", "implied", "explicit"): pay = "none"
        if cat not in CATEGORIES: cat = "other"
        rows.append({"id": cid, "is_signal": True, "problem": problem, "who": who, "workaround": "",
                     "pay": pay, "intensity": max(1, min(5, int(inten) if inten.isdigit() else 2)),
                     "category": cat, "mentioned_products": [], "chunk": k})
    with SIG.open("a") as f:
        for r in rows: f.write(json.dumps(r) + "\n")
    p["done"].append(k); PROG.write_text(json.dumps(p))
    print(f"chunk {k}: {len(rows)} signals saved, {bad} bad lines; {len(p['done'])}/{p['chunks']} chunks done")


def restratify():
    """Interleave unread comments by search phrase so every upcoming chunk is a mixed
    sample. init() puts the 'I would pay' phrases first, which skews early results
    toward consumer pay-to-remove-ads wishes."""
    p, ids = load()
    cs, done = p["chunk_size"], set(p["done"])
    com = comments()
    unread = [i for k in range(p["chunks"]) if k not in done for i in ids[k * cs:(k + 1) * cs]]
    by_phrase = {}
    for i in unread:  # keeps the existing priority order inside each phrase
        by_phrase.setdefault(com[i]["phrase"], []).append(i)
    queues = sorted(by_phrase.values(), key=len, reverse=True)
    mixed, n = [], max(map(len, queues))
    for j in range(n):  # spread each phrase evenly across the whole remaining run
        for q in queues:
            a, b = j * len(q) // n, (j + 1) * len(q) // n
            mixed.extend(q[a:b])
    it, new = iter(mixed), []
    for k in range(p["chunks"]):
        new.extend(ids[k * cs:(k + 1) * cs] if k in done else [next(it) for _ in ids[k * cs:(k + 1) * cs]])
    assert sorted(new) == sorted(ids)
    ORDER.write_text(json.dumps(new))
    print(f"re-ordered {len(unread)} unread comments across {len(queues)} phrases")


def extend():
    """Merge data/reddit/comments.jsonl (from fetch_reddit.py) into comments.jsonl, then
    append comments not yet in the order as extra chunks, mixed round-robin by search
    phrase. Inside a phrase, Reddit rows go by upvotes, others newest first."""
    p, ids = load()
    rd = DATA / "reddit" / "comments.jsonl"
    if rd.exists():
        com = comments()
        add = [l for l in rd.open() if json.loads(l)["id"] not in com]
        with (DATA / "comments.jsonl").open("a") as f:
            f.writelines(add)
        print(f"merged {len(add)} new Reddit comments into comments.jsonl")
    have, com = set(ids), comments()
    by_phrase = {}
    for i, c in com.items():
        if i not in have:
            by_phrase.setdefault(c.get("phrase", ""), []).append(i)
    if not by_phrase:
        print("nothing new to queue"); return
    for q in by_phrase.values():
        q.sort(key=lambda i: (-(com[i].get("score") or 0), -com[i]["t"]))
    queues, new = list(by_phrase.values()), []
    while any(queues):
        for q in queues:
            if q: new.append(q.pop(0))
    cs, last = p["chunk_size"], p["chunks"] - 1
    if len(ids) % cs and last in p["done"]:
        # appending would grow an already-read short chunk; never re-label read work
        sys.exit(f"last chunk {last} is short and already done; pad logic needed")
    start = len(ids)
    ids += new
    p["chunks"] = -(-len(ids) // cs)
    ORDER.write_text(json.dumps(ids))
    PROG.write_text(json.dumps(p))
    print(f"queued {len(new)} new comments as chunks {start // cs}..{p['chunks'] - 1} (now {p['chunks']} chunks)")


def status():
    p, _ = load()
    n = sum(1 for _ in SIG.open()) if SIG.exists() else 0
    print(f"{len(p['done'])}/{p['chunks']} chunks done, {n} signals")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init": init(int(sys.argv[3]) if "--chunk" in sys.argv else 150)
    elif cmd == "next": next_chunk()
    elif cmd == "save": save(sys.argv[2], sys.argv[3])
    elif cmd == "restratify": restratify()
    elif cmd == "extend": extend()
    else: status()
