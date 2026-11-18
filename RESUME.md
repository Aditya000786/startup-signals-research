# Status and how to resume

_Last updated: 2026-11-18._

## Where things stand

| Stage | Status |
|---|---|
| 1. Fetch (`fetch.py`) | Done. 38,273 comments from 37 search phrases, 2007-2026, in `data/comments.jsonl.gz` |
| 2. Extract (`ralph.py`, Pro-only) | **28 of 256 chunks done.** 4,200 comments read, 2,887 signals in `data/signals.jsonl` |
| 3a. Cluster (`local_cluster.py`) | Built and run on the 2,887 signals: 296 clusters of 3+ commenters |
| 3b. Idea cards (`cards.json` + `make_ideas.py`) | 21 hand-written cards covering 27 clusters, plus 40 draft clusters with no card yet |
| 4. Site (`build_site.py`) | Built: `site/index.html` (ideas) and `site/signals.html` (raw signals and rejected comments) |

All of this runs with no Anthropic API key. Claude does the reading in a Claude
Code session (Pro plan), and clustering uses a small local embedding model.

## Quality check, read this first

What the first 28 chunks show:

- **Phrase bias.** `ralph.py init` put the "I would pay / take my money" phrases
  first. As a result the 4,200 comments read so far are about 90% pay phrases, so the
  top ideas skew toward consumer "let me pay to remove ads" wishes about incumbents
  (YouTube, Firefox, search, news). Those wishes are real, but they are hard for a
  startup to act on. **I ran `python ralph.py restratify`**, so every upcoming chunk now
  mixes all 37 phrases ("I wish there was", "is a nightmare", "a huge pain", "why is
  there no", ...). The 28 read chunks are unchanged, and the old order is saved in
  `data/ralph_order.before_restratify.json`.
- **Extraction was lenient.** 69% of comments read were kept as signals, and `pay` is
  "explicit" for 75% of them, mostly because the search phrase itself says "pay".
  Intensity sits mostly at 3. If the raw-signals page shows too many weak
  signals, tighten the rule for the next chunks, for example by requiring a named
  workflow or a named product that falls short, and by keeping "explicit" for an
  actual price or a clear commitment.
- **How to check it yourself:** open `site/signals.html`, switch "Show" to *Rejected
  comments* to look for missed needs, and use *Not in any idea* to see signals that
  no card covers.

## Resume the extraction (Pro-only loop)

In a new Claude Code session with this repo:

```bash
pip install scikit-learn fastembed
gunzip -k data/comments.jsonl.gz
python ralph.py status        # should say 28/256 chunks done
```

Then start the loop with this prompt:

> /loop Ralph loop: HN idea miner, Pro-only extraction. Project dir: <path to this repo>. Each iteration: run `python3 ralph.py status`; if it shows 256/256 chunks done, stop. Otherwise process 3 chunks: for each, run `python3 ralph.py next`, read every comment, and write data/tsv/chunk_<k>.tsv with one line per comment that describes a concrete, unmet or badly met need (skip jokes, rants, vague complaints, needs met by a well-known free tool). Columns, tab-separated: id, problem, who, pay (none|implied|explicit), intensity (1-5), category (one of the CATEGORIES in extract.py). Then `python3 ralph.py save <k> data/tsv/chunk_<k>.tsv`. After the 3 chunks, commit and push data/ progress, report one line, and schedule the next iteration.

Each chunk is about 50k tokens of reading. The remaining 228 chunks will take many
Pro usage windows, and the loop resumes from disk every time.

**Faster alternative:** with an API key, `python extract.py` does the same job on
Haiku. It skips ids already in `signals.jsonl`, and the full remainder costs about $5-10.

## Rebuild the outputs at any point

```bash
python local_cluster.py            # data/clusters.json (embeddings; --tfidf to skip the model)
python make_ideas.py               # merges cards.json with clusters -> data/ideas.json
python build_site.py               # site/index.html + site/signals.html
```

To add or refresh idea cards, ask Claude to read the top draft clusters (the
"Show drafts" button on the site, or `data/clusters.json`) and append cards to
`cards.json`, using the same fields as `synthesize.py`'s `Idea` model plus
`anchor_ids` (3+ comment ids from the cluster). A cluster attaches to the card that
holds at least 2 of its member ids, so cards survive re-clustering on more data.
With an API key, `python synthesize.py` writes cards with Opus instead.

## Left to do

1. Review `site/signals.html` and decide whether to tighten the extraction rules.
2. Finish extraction: 228 chunks.
3. Re-run clustering, and write cards for any new top clusters, especially the non-pay
   phrases that are now coming through.
4. Optional: filter out repeat comments from one author in a single thread, which inflate
   some clusters (Firefox and YouTube).
