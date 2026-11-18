"""Stage 4: render data/ideas.json into a single static page, site/index.html.

No server needed: open the file, or upload it anywhere static (VPS, GitHub Pages,
Netlify). Per-viewer state (starred / dismissed ideas) lives in localStorage.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "ideas.json"
ideas = json.loads(src.read_text())
for i, x in enumerate(ideas):
    x["key"] = f"i{i}-{x['name'][:24]}"
    x.pop("sources", None)  # keep page small; quotes carry the evidence

TEMPLATE = (ROOT / "site_template.html").read_text()
out = ROOT / "site" / "index.html"
out.parent.mkdir(exist_ok=True)
payload = json.dumps(ideas, ensure_ascii=False).replace("</", "<\\/")
out.write_text(TEMPLATE.replace("/*IDEAS*/[]", payload))
print(f"{len(ideas)} ideas -> {out}")
