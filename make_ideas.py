"""Pro-only stage 3b: merge hand-written idea cards (cards.json) with the clusters
from local_cluster.py into data/ideas.json, in the format build_site.py expects.

cards.json is written by Claude in a Claude Code session (no API), playing the role
synthesize.py gives Opus. Each card names `anchor_ids`, comment ids that belong to
the theme. After re-clustering on more data, every cluster is attached to the card
whose anchors it contains most, so cards survive re-runs. A card with "status": "rejected" (plus
"reject_reason", "reject_kind") records a reviewed-and-declined theme: it claims its
clusters the same way, so they never reappear as drafts. Clusters with no card are
added as "draft" ideas (no analyst score) so you can see what still needs a card.

  python make_ideas.py [--drafts 40]
"""
import argparse, json, time
from collections import Counter
from pathlib import Path

from local_cluster import score

ROOT = Path(__file__).parent
DATA = ROOT / "data"


def quote(cid, com):
    c = com[cid]
    return {"id": cid, "author": c["author"], "year": time.gmtime(c["t"]).tm_year,
            "thread": c.get("story_title"), "text": c["text"][:700], "url": c.get("url")}


def with_reddit(picked, members, sig, com, n):
    """Keep the picked quotes but make sure up to 2 Reddit comments are shown when the idea
    has Reddit evidence (hand-picked card quotes predate the Reddit source)."""
    rd = sorted((m for m in members if m.startswith("rd_") and m in com and m not in picked[:n]),
                key=lambda m: (-sig[m]["intensity"], -com[m]["t"]))
    have = sum(m.startswith("rd_") for m in picked[:n])
    extra = rd[: max(0, 2 - have)]
    return [m for m in picked if m not in extra][: n - len(extra)] + extra


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", type=int, default=40, help="also list this many top clusters that have no card yet")
    ap.add_argument("--min-draft-authors", type=int, default=5,
                    help="HN-only clusters need this many distinct commenters to be listed as a draft")
    a = ap.parse_args()
    com = {c["id"]: c for c in map(json.loads, (DATA / "comments.jsonl").open())}
    sig = {s["id"]: s for s in map(json.loads, (DATA / "signals.jsonl").open()) if s["id"] in com}
    clusters = json.loads((DATA / "clusters.json").read_text())
    cards = json.loads((ROOT / "cards.json").read_text())

    owner = {}  # cluster index -> card index
    for ci, c in enumerate(clusters):
        mem = set(c["member_ids"])
        hits = [(len(mem & set(card["anchor_ids"])), k) for k, card in enumerate(cards)]
        best = max(hits)
        if best[0] >= 2:  # one stray anchor is not enough to claim a whole cluster
            owner[ci] = best[1]

    ideas = []
    for k, card in enumerate(cards):
        cl = [clusters[ci] for ci, o in owner.items() if o == k]
        members = sorted({m for c in cl for m in c["member_ids"]} | {m for m in card["anchor_ids"] if m in sig})
        if not members:
            continue  # anchors fell out of the strong-signal set and no cluster matched
        st = score(members, sig, com)
        cat = Counter(sig[m]["category"] for m in members).most_common(1)[0][0]
        if card.get("status") == "rejected":
            # reviewed and turned down: keeps claiming its clusters so they never come back as
            # drafts, and the page can still show why it was rejected
            ideas.append({"name": card["name"], "one_liner": card["reject_reason"], "rejected": True,
                          "reject_kind": card.get("reject_kind", "other"), "exists_today": card.get("exists_today", ""),
                          "verdict_score": None, "cluster": " + ".join(c["title"] for c in cl) or "(anchors only)",
                          "category": cat, "signal_score": st["signal_score"], "authors": st["authors"],
                          "years": st["years"], "pay_signals": st["pay_signals"], "avg_intensity": st["avg_intensity"],
                          "mentions": len(members), "quotes": [quote(q, com) for q in with_reddit(members, members, sig, com, 2)],
                          "sources": members})
            continue
        fields = {f: card.get(f, "") for f in ("name", "one_liner", "problem", "customer", "evidence", "solution", "mvp",
                                               "business_model", "why_now", "risks", "verdict_score")}
        # "What exists today and why it falls short" (older cards call it "alternatives")
        fields["exists_today"] = card.get("exists_today") or card.get("alternatives", "")
        qs = [q for q in card.get("quote_ids", []) if q in com] or members[:3]
        ideas.append({**fields, "cluster": " + ".join(c["title"] for c in cl) or "(anchors only)",
                      "category": cat, "signal_score": st["signal_score"], "authors": st["authors"],
                      "years": st["years"], "pay_signals": st["pay_signals"], "avg_intensity": st["avg_intensity"],
                      "mentions": len(members), "quotes": [quote(q, com) for q in with_reddit(qs, members, sig, com, 5)], "sources": members})
        if not cl:
            print(f"  warning: card '{card['name']}' matched no cluster; using its anchors only")

    free = [c for ci, c in enumerate(clusters) if ci not in owner]
    # Tightened: an unreviewed cluster is only worth a draft if 5+ distinct people raised it, or it
    # has Reddit evidence (Reddit is the newer, smaller source, so its clusters stay visible).
    rd = lambda c: any(m.startswith("rd_") for m in c["member_ids"])
    worth = [c for c in free if rd(c) or c["authors"] >= a.min_draft_authors]
    drafts = worth[: a.drafts] + [c for c in worth[a.drafts:] if rd(c)]
    for c in drafts:
        best = sorted(c["member_ids"], key=lambda m: (-sig[m]["intensity"], -com[m]["t"]))
        ideas.append({"name": c["problem"], "one_liner": "Draft: cluster not yet reviewed. Keywords: " + ", ".join(c["keywords"]),
                      "draft": True, "verdict_score": None, "cluster": c["title"], "category": c["category"],
                      "signal_score": c["signal_score"], "authors": c["authors"], "years": c["years"],
                      "pay_signals": c["pay_signals"], "avg_intensity": c["avg_intensity"], "mentions": len(c["members"]),
                      "problem": "; ".join(sig[m]["problem"] for m in best[:6]),
                      "quotes": [quote(m, com) for m in with_reddit(best, c["member_ids"], sig, com, 4)], "sources": c["members"]})

    (DATA / "ideas.json").write_text(json.dumps(ideas, indent=1, ensure_ascii=False))
    rej = sum(c.get("status") == "rejected" for c in cards)
    print(f"{len(cards) - rej} cards + {rej} rejected from {len(owner)} clusters, plus {len(drafts)} draft clusters -> data/ideas.json")


if __name__ == "__main__":
    main()
