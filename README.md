# HN Idea Miner

Finds startup ideas in the problems Hacker News commenters keep describing.
It works like Karpathy's HackerTube, which mined HN comments for the videos people
keep recommending. This tool mines them for unmet needs instead.

```
fetch.py       HN Algolia API: comments containing ~40 "pain / wish / I'd pay" phrases, 2007 to now
extract.py     Claude Haiku 5.5 reads each comment and returns: real need? problem, who, pay signal, pain 1-5, category
synthesize.py  Claude Opus 5.5 clusters the problems by category, scores the clusters, writes idea cards for the top N
build_site.py  writes site/index.html, a static page of ranked idea cards with source quotes linking back to HN
```

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...     # fetch.py works without it
python fetch.py                  # ~10 min, free
python extract.py --limit 400    # cheap test run first
python extract.py                # all comments
python synthesize.py --top 60
python build_site.py && open site/index.html
```

Every stage is resumable and writes to `data/`. Delete a stage's output file to
redo that stage.

## Signal score

A cluster scores higher when it has:
- more distinct commenters (log scale, so one loud thread can't dominate)
- explicit or implied willingness to pay
- higher average pain
- mentions in the last 3 years
- mentions spread across many years

The analyst score (out of 10) is Opus's own sceptical rating of the idea card. The
two scores often disagree, and a large gap between them is worth a closer look.

## Tuning

- Add more phrases to `PHRASES` in `fetch.py`, for example domain-specific ones
  like "as a nurse" or "our accountant".
- To run only one domain, pass `--phrases` or filter `signals.jsonl` by category
  before running `synthesize.py`.
- For full coverage like HackerTube's 43M-comment scan, replace `fetch.py` with a
  query on `bigquery-public-data.hacker_news.full`. The later stages take the same
  JSONL format.
