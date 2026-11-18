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


def status():
    p, _ = load()
    n = sum(1 for _ in SIG.open()) if SIG.exists() else 0
    print(f"{len(p['done'])}/{p['chunks']} chunks done, {n} signals")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init": init(int(sys.argv[3]) if "--chunk" in sys.argv else 150)
    elif cmd == "next": next_chunk()
    elif cmd == "save": save(sys.argv[2], sys.argv[3])
    else: status()
