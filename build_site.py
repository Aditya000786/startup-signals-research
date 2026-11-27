"""Stage 4: render the results into a static two-page site.

  site/index.html    ranked idea cards (data/ideas.json), with source quotes linking to HN / Reddit
  site/signals.html  every comment read so far: the extracted problem next to the
                     original comment, plus the comments that were rejected, so you
                     can judge extraction quality by eye

No server needed: open the files, or upload the folder anywhere static. Per-viewer
state (starred / hidden ideas) lives in localStorage.
"""
import json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUT = ROOT / "site"


def embed(template, **slots):
    html = (ROOT / template).read_text()
    for k, v in slots.items():
        html = html.replace(f"/*{k}*/" + ("[]" if isinstance(v, list) else "{}"),
                            json.dumps(v, ensure_ascii=False).replace("</", "<\\/"))
    return html


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "ideas.json"
    ideas = json.loads(src.read_text())
    com = {c["id"]: c for c in map(json.loads, (DATA / "comments.jsonl").open())}
    sig = {s["id"]: s for s in map(json.loads, (DATA / "signals.jsonl").open()) if s["id"] in com}

    # Which comments were actually read: ralph chunks if present, else everything extract.py saw.
    prog, order = DATA / "progress.json", DATA / "ralph_order.json"
    if prog.exists() and order.exists():
        p, ids = json.loads(prog.read_text()), json.loads(order.read_text())
        read = [i for k in sorted(p["done"]) for i in ids[k * p["chunk_size"]:(k + 1) * p["chunk_size"]] if i]
        ids = [i for i in ids if i]  # drop chunk padding
        note = (f"{len(p['done'])} of {p['chunks']} chunks read ({len(read):,} of {len(ids):,} comments), "
                f"{len(sig):,} signals extracted.")
    else:
        read, note = list(sig), f"{len(sig):,} signals."

    idea_of = {}
    for x in ideas:
        if not x.get("draft") and not x.get("rejected"):
            for m in x.get("sources", []):
                idea_of.setdefault(m, x["name"])
    for i, x in enumerate(ideas):
        x["key"] = f"i{i}-{x['name'][:24]}"
        mem = x.get("sources", [])
        rd = sum(str(m).startswith("rd_") for m in mem)
        x["src"] = {"hn": len(mem) - rd, "reddit": rd}
        x.pop("sources", None)  # keep page small; quotes carry the evidence

    rows = []
    for cid in read:
        c, s = com[cid], sig.get(cid)
        r = {"id": cid, "yr": time.gmtime(c["t"]).tm_year, "t": (c.get("story_title") or "")[:50],
             "m": c.get("phrase", ""), "x": re.sub(r"\s+", " ", c["text"])[:150]}
        if str(cid).startswith("rd_") and c.get("story_id"):
            r["u"] = str(c["story_id"])[3:]  # post id; page builds the reddit permalink
        elif c.get("url"):
            r["u"] = c["url"]
        if s:
            r.update(p=s["problem"], w=s["who"], y=s["pay"], n=s["intensity"], c=s["category"], i=idea_of.get(cid))
        rows.append(r)

    OUT.mkdir(exist_ok=True)
    nd, nr = sum(bool(x.get("draft")) for x in ideas), sum(bool(x.get("rejected")) for x in ideas)
    parts = [f"{len(ideas) - nd - nr} idea cards", f"{nr} rejected themes"] + ([f"{nd} drafts"] if nd else [])
    meta = {"note": f"{note} {', '.join(parts)}. Built {time.strftime('%Y-%m-%d')}."}
    (OUT / "index.html").write_text(embed("site_template.html", IDEAS=ideas, META=meta))
    (OUT / "signals.html").write_text(embed("signals_template.html", ROWS=rows, META=meta))
    print(f"{len(ideas)} ideas -> {OUT/'index.html'}; {len(rows)} comments -> {OUT/'signals.html'}")


if __name__ == "__main__":
    main()
