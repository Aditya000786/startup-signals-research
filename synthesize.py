"""Stage 3: Claude Opus 5.5 groups the extracted problems into recurring themes,
scores each theme by how often / how painfully / how recently it comes up, and
writes a startup idea card for the strongest themes.

Outputs data/clusters.json and data/ideas.json. Needs ANTHROPIC_API_KEY.
"""
import argparse, json, math, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import anthropic
from pydantic import BaseModel

DATA = Path(__file__).parent / "data"
MODEL = "claude-opus-5-5"
# Opus 5.5 can decline in rare safety categories; let the API retry on a fallback model.
OPUS = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")


class Cluster(BaseModel):
    title: str  # short name of the recurring problem
    problem: str  # 1-2 sentences, general enough to cover all members
    member_ids: List[str]


class Clusters(BaseModel):
    clusters: List[Cluster]


class Idea(BaseModel):
    name: str  # a plausible product name
    one_liner: str
    problem: str
    customer: str  # who buys it, as specific as the evidence allows
    evidence: str  # what the comments show: volume, intensity, money, workarounds
    solution: str
    mvp: str  # what to build first, in a few weeks
    business_model: str
    alternatives: str  # incumbents / tools commenters mention and why they fall short
    why_now: str
    risks: str
    verdict_score: int  # 1-10, your honest overall rating of the opportunity
    quote_ids: List[str]  # 3-5 comment ids that best show the pain


CLUSTER_SYS = """You group startup problem statements mined from Hacker News comments.

Each line is `id | problem | who | pay | intensity`. Merge lines that describe the
same underlying need, even if the wording differs. Make each cluster specific
enough that one product could serve it (not "developers want better tools").
Drop singletons that fit nowhere. Every id may appear in at most one cluster.
Return clusters with at least 3 members."""

IDEA_SYS = """You are a sharp, sceptical startup analyst. You get one recurring problem
mined from Hacker News comments, with the raw comments as evidence. Write a startup
idea card grounded ONLY in that evidence: quote patterns, not imagined demand. Be
concrete about the customer and the first version. Name existing products the
commenters mention and why they fall short. Score the opportunity honestly: most
ideas are 4-6; reserve 8+ for strong, paid, recurring, under-served pain."""


def cluster_category(client, cat, rows):
    lines = "\n".join(
        f"{r['id']} | {r['problem']} | {r['who']} | {r['pay']} | {r['intensity']}" for r in rows
    )
    with client.beta.messages.stream(
        model=MODEL, max_tokens=64000, system=CLUSTER_SYS,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": f"Category: {cat}\n\n{lines}"}],
        output_format=Clusters, **OPUS,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal" or msg.parsed_output is None:
        print(f"  {cat}: no clusters ({msg.stop_reason})")
        return []
    out = []
    for c in msg.parsed_output.clusters:
        out.append({"category": cat, **c.model_dump()})
    print(f"  {cat}: {len(rows)} problems -> {len(out)} clusters")
    return out


def score(cluster, sig, com):
    members = [m for m in cluster["member_ids"] if m in sig and m in com]
    authors = {com[m]["author"] for m in members}
    years = {time.gmtime(com[m]["t"]).tm_year for m in members}
    pay = sum(sig[m]["pay"] == "explicit" for m in members) + 0.5 * sum(sig[m]["pay"] == "implied" for m in members)
    intensity = sum(sig[m]["intensity"] for m in members) / max(len(members), 1)
    newest = max((com[m]["t"] for m in members), default=0)
    recent = sum(com[m]["t"] > time.time() - 3 * 365 * 86400 for m in members)
    s = (math.log1p(len(authors)) * 3 + math.log1p(pay) * 2 + intensity
         + math.log1p(recent) * 1.5 + min(len(years), 10) * 0.3)
    return {
        "members": members, "authors": len(authors), "years": sorted(years),
        "pay_signals": pay, "avg_intensity": round(intensity, 2),
        "recent_mentions": recent, "newest": newest, "signal_score": round(s, 2),
    }


def write_idea(client, cluster, com):
    ev = sorted(cluster["members"], key=lambda m: -com[m]["t"])[:30]
    body = "\n\n".join(
        f"<comment id=\"{m}\" year=\"{time.gmtime(com[m]['t']).tm_year}\">\n{com[m]['text'][:1800]}\n</comment>"
        for m in ev
    )
    stats = (f"{cluster['authors']} distinct commenters, years {cluster['years'][0]}-{cluster['years'][-1]}, "
             f"{cluster['pay_signals']} willingness-to-pay signals, avg intensity {cluster['avg_intensity']}/5")
    with client.beta.messages.stream(
        model=MODEL, max_tokens=16000, system=IDEA_SYS,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content":
                   f"Recurring problem: {cluster['title']}\n{cluster['problem']}\nStats: {stats}\n\n{body}"}],
        output_format=Idea, **OPUS,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal" or msg.parsed_output is None:
        return None
    return msg.parsed_output.model_dump()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60, help="write idea cards for this many clusters")
    ap.add_argument("--max-per-call", type=int, default=3000, help="problems per clustering call")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    com = {c["id"]: c for c in map(json.loads, (DATA / "comments.jsonl").open())}
    sig = {s["id"]: s for s in map(json.loads, (DATA / "signals.jsonl").open()) if s["is_signal"]}
    print(f"{len(sig)} signals from {len(com)} comments")

    client = anthropic.Anthropic(max_retries=5)
    cpath = DATA / "clusters.json"
    if cpath.exists():
        clusters = json.loads(cpath.read_text())
        print(f"reusing {len(clusters)} clusters from {cpath.name} (delete it to recluster)")
    else:
        by_cat = defaultdict(list)
        for s in sig.values():
            by_cat[s["category"]].append(s)
        jobs = []
        for cat, rows in by_cat.items():
            rows.sort(key=lambda r: -com[r["id"]]["t"])
            for i in range(0, len(rows), args.max_per_call):
                jobs.append((cat, rows[i : i + args.max_per_call]))
        with ThreadPoolExecutor(args.workers) as pool:
            clusters = [c for res in pool.map(lambda j: cluster_category(client, *j), jobs) for c in res]
        for c in clusters:
            c.update(score(c, sig, com))
        clusters = [c for c in clusters if c["authors"] >= 3]
        clusters.sort(key=lambda c: -c["signal_score"])
        cpath.write_text(json.dumps(clusters, indent=1))

    top = clusters[: args.top]
    print(f"writing {len(top)} idea cards")
    with ThreadPoolExecutor(args.workers) as pool:
        cards = list(pool.map(lambda c: write_idea(client, c, com), top))
    ideas = []
    for c, card in zip(top, cards):
        if not card:
            continue
        quotes = [q for q in card.pop("quote_ids") if q in com][:5] or c["members"][:3]
        ideas.append({
            **card, "cluster": c["title"], "category": c["category"],
            "signal_score": c["signal_score"], "authors": c["authors"], "years": c["years"],
            "pay_signals": c["pay_signals"], "avg_intensity": c["avg_intensity"],
            "mentions": len(c["members"]),
            "quotes": [{"id": q, "author": com[q]["author"], "year": time.gmtime(com[q]["t"]).tm_year,
                        "thread": com[q].get("story_title"), "text": com[q]["text"][:700], "url": com[q].get("url")} for q in quotes],
            "sources": c["members"],
        })
    (DATA / "ideas.json").write_text(json.dumps(ideas, indent=1))
    print(f"wrote {len(ideas)} ideas -> data/ideas.json")


if __name__ == "__main__":
    main()
