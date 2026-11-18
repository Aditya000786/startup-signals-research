"""Pro-only stage 3a: cluster the extracted problems locally, with no API calls.

Same output format as synthesize.py's clustering step (data/clusters.json), so the
API-based idea-card step, or a hand-written one, can run on top of it.

Method: embed each problem sentence with a small local model (BAAI/bge-small-en-v1.5
via fastembed, ~130MB, downloaded once from Hugging Face; falls back to TF-IDF if
fastembed is missing), then agglomerative clustering with cosine distance and
average linkage. TF-IDF is still used to label each cluster with keywords. Clusters with fewer than
--min-authors distinct commenters are dropped. Scoring is the same signal score that
synthesize.py uses.

  pip install scikit-learn fastembed
  python local_cluster.py [--threshold 0.25] [--min-authors 3]
"""
import argparse, json, math, re, time
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

DATA = Path(__file__).parent / "data"
# Words that appear in almost every problem sentence and say nothing about the need.
NOISE = {"users", "user", "people", "want", "wants", "would", "pay", "need", "needs", "good",
         "better", "lack", "lacks", "struggle", "struggles", "like", "use", "using", "real",
         "way", "find", "make", "makes", "without", "instead", "able", "month", "year", "money",
         "happily", "explicit", "implied", "tool", "service", "app", "apps", "version", "free"}
STOP = list(ENGLISH_STOP_WORDS | NOISE)


def score(members, sig, com):
    """Identical to synthesize.score: distinct authors, pay, pain, recency, year spread."""
    authors = {com[m]["author"] for m in members}
    years = {time.gmtime(com[m]["t"]).tm_year for m in members}
    pay = sum(sig[m]["pay"] == "explicit" for m in members) + 0.5 * sum(sig[m]["pay"] == "implied" for m in members)
    intensity = sum(sig[m]["intensity"] for m in members) / max(len(members), 1)
    recent = sum(com[m]["t"] > time.time() - 3 * 365 * 86400 for m in members)
    s = (math.log1p(len(authors)) * 3 + math.log1p(pay) * 2 + intensity
         + math.log1p(recent) * 1.5 + min(len(years), 10) * 0.3)
    return {"members": members, "authors": len(authors), "years": sorted(years),
            "pay_signals": pay, "avg_intensity": round(intensity, 2), "recent_mentions": recent,
            "newest": max(com[m]["t"] for m in members), "signal_score": round(s, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=None,
                    help="cosine distance cut-off, lower = tighter (default 0.25 embeddings, 0.8 TF-IDF)")
    ap.add_argument("--tfidf", action="store_true", help="skip embeddings, cluster on TF-IDF")
    ap.add_argument("--min-authors", type=int, default=3)
    a = ap.parse_args()

    com = {c["id"]: c for c in map(json.loads, (DATA / "comments.jsonl").open())}
    sig = {s["id"]: s for s in map(json.loads, (DATA / "signals.jsonl").open()) if s.get("is_signal") and s["id"] in com}
    ids = list(sig)
    texts = [sig[i]["problem"] for i in ids]
    print(f"{len(ids)} signals")

    vec = TfidfVectorizer(stop_words=STOP, ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    X = vec.fit_transform(texts)
    emb = None
    if not a.tfidf:
        try:
            from fastembed import TextEmbedding
            emb = np.array(list(TextEmbedding("BAAI/bge-small-en-v1.5").embed(texts, batch_size=256)))
            print("clustering on local embeddings")
        except ImportError:
            print("fastembed not installed, clustering on TF-IDF")
    if emb is not None:
        keep = np.ones(len(ids), bool)
        feats, thr = emb, a.threshold or 0.25
    else:
        keep = np.asarray(X.sum(axis=1)).ravel() > 0  # sentences made only of rare words can't cluster
        feats, thr = X[keep].toarray(), a.threshold or 0.8
    Xd = X[keep].toarray()
    kids = [i for i, k in zip(ids, keep) if k]
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=thr).fit_predict(feats)
    terms = np.array(vec.get_feature_names_out())

    clusters = []
    for lab in set(labels):
        idx = np.where(labels == lab)[0]
        members = [kids[j] for j in idx]
        st = score(members, sig, com)
        if st["authors"] < a.min_authors:
            continue
        centroid = Xd[idx].mean(axis=0)
        top = [t for t in terms[np.argsort(-centroid)[:6]]]
        f = (feats if emb is not None else Xd)[idx]
        central = members[int(np.argmax(f @ f.mean(axis=0)))]
        cat = Counter(sig[m]["category"] for m in members).most_common(1)[0][0]
        clusters.append({"title": ", ".join(top[:4]), "keywords": top, "problem": sig[central]["problem"],
                         "category": cat, "member_ids": members, **st})
    clusters.sort(key=lambda c: -c["signal_score"])
    (DATA / "clusters.json").write_text(json.dumps(clusters, indent=1))
    covered = sum(len(c["members"]) for c in clusters)
    print(f"{len(clusters)} clusters (>= {a.min_authors} authors) covering {covered}/{len(ids)} signals -> data/clusters.json")
    for c in clusters[:40]:
        print(f"{c['signal_score']:6.2f} {c['authors']:3d}a {c['pay_signals']:5.1f}$  [{c['category']}] {c['title']}  | {c['problem'][:90]}")


if __name__ == "__main__":
    main()
