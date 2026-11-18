"""Stage 2: Claude Haiku 5.5 reads every comment and extracts the problem in it.

Comments are sent in batches; each comment gets a structured verdict:
is it a real, specific, unmet need? who has it, how painful, would they pay?
Results go to data/signals.jsonl. Resumable: already-judged ids are skipped.

Needs ANTHROPIC_API_KEY in the environment.
"""
import argparse, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Literal

import anthropic
from pydantic import BaseModel

DATA = Path(__file__).parent / "data"
IN, OUT = DATA / "comments.jsonl", DATA / "signals.jsonl"
MODEL = "claude-haiku-5-5"

CATEGORIES = [
    "developer tools", "devops & infra", "data & analytics", "AI & ML tooling",
    "security & privacy", "productivity & work", "SMB & back-office", "finance & money",
    "health & fitness", "education & learning", "consumer & home", "media & content",
    "commerce & marketplaces", "hardware & IoT", "hiring & careers", "legal & government",
    "communication & social", "travel & local", "other",
]


class Verdict(BaseModel):
    id: str
    is_signal: bool  # a concrete, unmet need a product could address
    problem: str  # one sentence, in neutral words, empty if not a signal
    who: str  # who has the problem (role / kind of company / person)
    workaround: str  # what they do today, "" if unknown
    pay: Literal["none", "implied", "explicit"]
    intensity: int  # 1 mild annoyance .. 5 hair-on-fire
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
    mentioned_products: List[str]  # existing products named as inadequate/alternatives


class BatchVerdict(BaseModel):
    verdicts: List[Verdict]


SYSTEM = f"""You read Hacker News comments and pick out startup-grade problems.

For each comment, decide whether the author describes a concrete, unmet (or badly
met) need that a product or service could address: something they or people like
them actually experience. Mark is_signal=false for jokes, hypotheticals with no
felt pain, rants about politics or companies with no product angle, requests
already answered by a well-known free tool, and anything too vague to build for.

When it is a signal, write `problem` as one plain sentence that someone could
build against (no quotes, no usernames). `who` is the person or organisation that
has the problem. `pay`: explicit if they say they would pay or already pay;
implied if they spend real money or hours on it; otherwise none. `intensity` from
1 (mild annoyance) to 5 (costing them serious time or money, repeatedly).
Pick the single best category from: {", ".join(CATEGORIES)}.

Return exactly one verdict per comment, using the comment's id."""


def judge(client, batch):
    body = "\n\n".join(
        f"<comment id=\"{c['id']}\" thread=\"{(c.get('story_title') or '')[:120]}\">\n{c['text'][:2500]}\n</comment>"
        for c in batch
    )
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": body}],
        output_format=BatchVerdict,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        return []
    ids = {c["id"] for c in batch}
    return [v.model_dump() for v in resp.parsed_output.verdicts if v.id in ids]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="only judge this many comments (for a test run)")
    args = ap.parse_args()

    comments = [json.loads(l) for l in IN.open()]
    done = {json.loads(l)["id"] for l in OUT.open()} if OUT.exists() else set()
    todo = [c for c in comments if c["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"{len(comments)} comments, {len(done)} already judged, {len(batches)} batches to go")

    client = anthropic.Anthropic(max_retries=5)
    signals = 0
    with OUT.open("a") as f, ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(judge, client, b) for b in batches]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                rows = fut.result()
            except anthropic.APIError as e:
                print(f"  batch failed, will retry on next run: {e}")
                continue
            for r in rows:
                f.write(json.dumps(r) + "\n")
                signals += r["is_signal"]
            f.flush()
            if i % 25 == 0 or i == len(batches):
                print(f"  {i}/{len(batches)} batches, {signals} signals so far")


if __name__ == "__main__":
    main()
