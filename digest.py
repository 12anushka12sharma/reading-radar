#!/usr/bin/env python3
"""Render items.json into a readable digest, and optionally email it.

Writes digest.html (what you actually read) and digest.md (greppable, and what
gets pasted into Claude if you want a summary pass). Both are gitignored.

  python3 digest.py             # render only
  python3 digest.py --email     # render, then send (needs .env, see README)
  python3 digest.py --open      # render, then open in the default browser
"""
import argparse
import hashlib
import json
import os
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config

TODAY = datetime.now().strftime("%a %d %b %Y")

KIND_LABEL = {"paper": "paper", "blog": "blog", "news": "news", "discussion": "discussion"}


def load_env():
    """Read KEY=VALUE from .env next to this script. Never committed — see .gitignore."""
    env = {}
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return {**env, **os.environ}


def topic_labels(interests):
    return {t["name"]: t.get("label", t["name"]) for t in interests["topics"]}


# Acronyms that must not be title-cased into "Aeo" / "Llm".
_ACRONYMS = {"aeo", "geo", "llm", "ai", "cx", "ml", "seo"}


def short_label(topic):
    """A chip-sized name derived from the topic key.

    Full labels can run long enough to make the filter bar taller than the
    masthead. The key
    itself is already short and unambiguous.
    """
    parts = topic.split("_")
    words = [p.upper() if p in _ACRONYMS else p.capitalize() for p in parts]
    # aeo_geo reads better as a pair than as two words
    if len(words) == 2 and all(p in _ACRONYMS for p in parts):
        return "/".join(words)
    return " ".join(words)


def group(items, interests):
    """Group by the item's strongest topic, in profile-weight order."""
    order = [t["name"] for t in sorted(interests["topics"],
                                       key=lambda t: -t.get("weight", 0))]
    rank = {n: i for i, n in enumerate(order)}
    buckets = {}
    for it in items:
        primary = min(it["topics"], key=lambda t: rank.get(t, 99)) if it["topics"] else "other"
        buckets.setdefault(primary, []).append(it)
    return sorted(buckets.items(), key=lambda kv: rank.get(kv[0], 99))


# ---------------------------------------------------------------------------
# Where a link actually goes — shared by both renderers
# ---------------------------------------------------------------------------

def destination(url):
    """Hostname a link actually lands on, for showing next to the title."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def link_line(it):
    """Where this item really goes, as its own line under the headline.

    Half the digest comes from Google News, whose links are
    news.google.com redirects rather than the publisher. The article id is an
    opaque Google token and the redirect serves a JS interstitial, so the real
    URL cannot be resolved with stdlib alone — but the feed carries
    `<source url>`, so the publisher can at least be named and linked directly.
    """
    host = destination(it["url"])
    pub_url = (it.get("extra") or {}).get("publisher_url")
    pub = (it.get("extra") or {}).get("publisher")
    if host == "news.google.com" and pub_url:
        return (f"<div class='dest'>via <span class='host'>news.google.com</span> → "
                f"<a href='{escape(pub_url)}' target='_blank' rel='noopener noreferrer'>"
                f"{escape(pub or destination(pub_url))}</a></div>")
    if host == "news.google.com":
        return "<div class='dest'>via <span class='host'>news.google.com</span></div>"
    return f"<div class='dest'><span class='host'>{escape(host)}</span></div>"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def tldr(summary, limit=760):
    """A readable brief, cut at a sentence boundary rather than mid-word.

    Feeds give wildly different amounts: arXiv and bioRxiv carry a full
    abstract, Science a single line, and Google News nothing usable at all —
    its RSS description is the headline repeated inside an anchor, which is
    stripped at ingest. So this shows as much as exists and stops cleanly;
    it cannot invent a summary for an item that shipped without one.
    """
    s = (summary or "").strip()
    if not s:
        return ""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # prefer the last sentence end; fall back to the last space
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > limit * 0.5:
        return cut[:end + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",;:") + "…"


def render_md(items, interests):
    labels = topic_labels(interests)
    L = [f"# Reading Radar — {TODAY}\n", f"{len(items)} items.\n"]
    for topic, group_items in group(items, interests):
        L.append(f"\n## {labels.get(topic, topic)}\n")
        for it in group_items:
            when = (it.get("published") or "")[:10]
            L.append(f"- **[{it['title']}]({it['url']})** · {it['source']}"
                     f"{' · ' + when if when else ''} · score {it['score']}")
            host = destination(it["url"])
            pub_url = (it.get("extra") or {}).get("publisher_url")
            if host == "news.google.com" and pub_url:
                L.append(f"  - link: via news.google.com → "
                         f"{(it['extra'].get('publisher') or destination(pub_url))} <{pub_url}>")
            elif host:
                L.append(f"  - link: {host}")
            if it.get("extra", {}).get("discussion"):
                L.append(f"  - discussion: {it['extra']['discussion']}")
            brief = tldr(it.get("summary"))
            if brief:
                L.append(f"  - TL;DR: {brief}")
            if it.get("hits"):
                L.append(f"  - matched: {', '.join(it['hits'][:6])}")
    L.append("\n---\n_Generated by work/reading-radar. Tune interests.json to change what shows up._")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Archive
#
# seen.json only remembers URLs for 45 days and carries no titles or scores, so
# it cannot answer "what did I read about in March". This keeps a permanent,
# append-only record instead.
#
# One flat JSON object per line, shaped like a database row rather than a
# nested blob: first_seen, url, title, source, score, topics, published. That
# shape is deliberate — if this ever becomes multi-user, these rows become a
# table with a user_id column and nothing else changes.
# ---------------------------------------------------------------------------

ARCHIVE = "archive.jsonl"


def load_archive():
    """Every archived row, oldest first. Tolerates a truncated final line."""
    path = HERE / ARCHIVE
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # a half-written line from an interrupted run costs one row, not the file
            print(f"  ! skipped an unreadable {ARCHIVE} line", file=sys.stderr)
    return rows


def append_archive(items):
    """Append today's items, skipping URLs already recorded.

    Dedupe is on URL and keeps the FIRST sighting: the archive answers when a
    piece first reached the digest, so a re-run on the same day must not move
    the date or double the row.
    """
    existing = {r.get("url") for r in load_archive()}
    today = datetime.now().strftime("%Y-%m-%d")
    new = []
    for it in items:
        if it["url"] in existing:
            continue
        existing.add(it["url"])
        new.append({
            "first_seen": today,
            "url": it["url"],
            "title": it["title"],
            "source": it["source"],
            "score": it["score"],
            "topics": it.get("topics", []),
            "published": (it.get("published") or "")[:10],
            "publisher_url": (it.get("extra") or {}).get("publisher_url", ""),
        })
    if new:
        with (HERE / ARCHIVE).open("a", encoding="utf-8") as fh:
            for row in new:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new)


# ---------------------------------------------------------------------------
# Pixel art
#
# Drawn as inline SVG rects rather than an icon font or image, for three
# reasons: it stays a single self-contained file, it needs no network (the
# digest is opened from disk, sometimes right after a 07:30 wake), and
# shape-rendering=crispEdges keeps the pixels genuinely hard-edged at any size.
# Authored as 8x8 character grids because editing art as text beats editing
# path data.
# ---------------------------------------------------------------------------

PIXEL_GLYPHS = {
    # a target / radar lock — answer-engine visibility
    "aeo_geo": ["..xxxx..", ".x....x.", "x..xx..x", "x.x..x.x",
                "x.x..x.x", "x..xx..x", ".x....x.", "..xxxx.."],
    # a little robot
    "llm_agents": [".xxxxxx.", "x......x", "x.x..x.x", "x......x",
                   "x.xxxx.x", "x......x", ".xxxxxx.", "..x..x.."],
    # speaker throwing sound
    "conversational_ai": ["...xx...", "..xxx...", ".xxxx.x.", "xxxxx..x",
                          "xxxxx..x", ".xxxx.x.", "..xxx...", "...xx..."],
    # speech bubble — support
    "cx_automation": ["xxxxxxx.", "x.....x.", "x.xxx.x.", "x.....x.",
                      "xxxxxxx.", "..x.....", ".x......", "........"],
    # a parcel
    "ecommerce_logistics": ["........", "xxxxxxxx", "x..xx..x", "x..xx..x",
                            "xxxxxxxx", "x..xx..x", "x..xx..x", "xxxxxxxx"],
    # conical flask
    "biotech_life_science": ["..xxxx..", "...xx...", "...xx...", "..xxxx..",
                             ".xxxxxx.", ".xxxxxx.", "xxxxxxxx", ".xxxxxx."],
    # atom
    "hard_science": ["...x....", "..xxx...", ".x.x.x..", "xxxxxxx.",
                     ".x.x.x..", "..xxx...", "...x....", "........"],
    # bar chart
    "product_craft": ["x.......", "x....xx.", "x..xxxx.", "x.xxxxx.",
                      "x.xxxxx.", "x.xxxxx.", "xxxxxxxx", "........"],
    # terminal window
    "devtools_eng": ["xxxxxxxx", "x......x", "x.xx...x", "x...xx.x",
                     "x.xx...x", "x......x", "xxxxxxxx", "........"],
    # connected nodes
    "ml_research": ["xx....xx", "xx....xx", "..x..x..", "...xx...",
                    "..x..x..", "xx....xx", "xx....xx", "........"],
    "_default": ["........", ".xxxxxx.", ".x....x.", ".x.xx.x.",
                 ".x.xx.x.", ".x....x.", ".xxxxxx.", "........"],
}

# The masthead mark: a dish with a sweep coming off it.
RADAR_MARK = [
    "......x.....",
    ".....xx.x...",
    "....xx..xx..",
    "...xx..x..x.",
    "..xx..x..x.x",
    ".xxxxxx.x..x",
    "..xxxx..x..x",
    "...xx..x..x.",
    "...xx.xx.x..",
    "..xxxx..xx..",
    ".xxxxxx..x..",
    "xxxxxxxx....",
]


def pixel_svg(rows, px=3, cls="px"):
    """Render an 'x'/'.' grid as an SVG of rects, merging horizontal runs.

    Run-merging matters: a naive rect-per-pixel version of this page was
    several times the markup for identical output.
    """
    h = len(rows)
    w = max(len(r) for r in rows)
    out = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            if row[x] == "x":
                run = 1
                while x + run < len(row) and row[x + run] == "x":
                    run += 1
                out.append(f"<rect x='{x}' y='{y}' width='{run}' height='1'/>")
                x += run
            else:
                x += 1
    return (f"<svg class='{cls}' viewBox='0 0 {w} {h}' width='{w * px}' "
            f"height='{h * px}' shape-rendering='crispEdges' fill='currentColor' "
            f"aria-hidden='true' focusable='false'>" + "".join(out) + "</svg>")


def topic_glyph(topic, px=3):
    return pixel_svg(PIXEL_GLYPHS.get(topic, PIXEL_GLYPHS["_default"]), px)


def item_cover(seed, topic, px=5):
    """A per-item pixel cover: unique texture behind the topic's own glyph.

    Deliberately generated rather than fetched. Real feed images lose on every
    axis that matters here: arXiv — the largest single source — carries none,
    so coverage would be visibly patchy; hotlinking makes the page call out to
    publishers every time it opens, which breaks offline use and leaks what is
    being read; and inlining them costs megabytes an issue. This is
    deterministic from the URL, so an item looks the same every time it
    appears, and it costs nothing.

    The texture is mirrored down the middle because symmetry reads as a mark
    rather than as noise.
    """
    seed_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
    size = 12
    half = size // 2
    rows = []
    for y in range(size):
        left = []
        for x in range(half):
            bit = seed_bytes[(y * half + x) % len(seed_bytes)]
            # bias toward sparse — roughly a third of cells filled
            left.append("x" if bit % 3 == 0 else ".")
        rows.append("".join(left) + "".join(reversed(left)))

    def runs(grid, dx=0, dy=0):
        out = []
        for y, row in enumerate(grid):
            x = 0
            while x < len(row):
                if row[x] == "x":
                    run = 1
                    while x + run < len(row) and row[x + run] == "x":
                        run += 1
                    out.append(f"<rect x='{x + dx}' y='{y + dy}' width='{run}' height='1'/>")
                    x += run
                else:
                    x += 1
        return "".join(out)

    glyph = PIXEL_GLYPHS.get(topic, PIXEL_GLYPHS["_default"])
    off = (size - len(glyph)) // 2
    return (f"<svg class='cover' viewBox='0 0 {size} {size}' width='{size * px}' "
            f"height='{size * px}' shape-rendering='crispEdges' aria-hidden='true' "
            f"focusable='false'>"
            f"<g class='cv-bg'>{runs(rows)}</g>"
            f"<g class='cv-fg'>{runs(glyph, off, off)}</g></svg>")


def score_meter(score):
    """Score as five pixel blocks. Scannable at a glance; the number stays too."""
    filled = max(1, min(5, int(round(float(score) / 2.0))))
    cells = "".join(f"<i class='{'on' if i < filled else 'off'}'></i>" for i in range(5))
    return f"<span class='meter' title='score {score}'>{cells}<b>{score}</b></span>"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
/* Reading Radar — a pixel broadsheet.
   Newspaper structure carries the reading; the retro styling is applied to
   furniture (masthead, section rules, badges, meters) and never to running
   text. Headlines and summaries stay in a real reading face at real sizes,
   because 24 dense research titles a day is a legibility problem first and a
   styling exercise second. */
:root {
  --paper:  #f3ede0;   --card:  #fbf7ee;   --ink:   #1b1a18;
  --muted:  #6a635a;   --rule:  #cdc2a9;   --inkSoft: #403b34;
  --red:    #b63a26;   --teal:  #15706a;   --gold:  #b07d12;
  --shadow: #1b1a18;
}
/* Dark palette defined once and referenced twice: the media query is what
   actually fires for a reader, the attribute selector makes it inspectable
   without changing OS settings. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #14161c;  --card: #1c1f27;    --ink:   #ece6d8;
    --muted: #9a948a;  --rule: #343a46;    --inkSoft: #c8c2b5;
    --red:   #ff7f63;  --teal: #54d8c4;    --gold:  #f0c050;
    --shadow: #000;
  }
}
:root[data-theme="dark"] {
  --paper: #14161c;  --card: #1c1f27;    --ink:   #ece6d8;
  --muted: #9a948a;  --rule: #343a46;    --inkSoft: #c8c2b5;
  --red:   #ff7f63;  --teal: #54d8c4;    --gold:  #f0c050;
  --shadow: #000;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 26px 16px 80px; color: var(--ink);
  background: var(--paper);
  /* faint halftone — the only texture, kept low so text stays crisp */
  background-image: radial-gradient(var(--rule) 0.5px, transparent 0.5px);
  background-size: 7px 7px;
  font: 16px/1.62 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
}
.wrap { max-width: 880px; margin: 0 auto; }
.px { display: block; }

/* ---- masthead ---------------------------------------------------------- */
.mast {
  background: var(--card); border: 3px solid var(--ink);
  box-shadow: 6px 6px 0 var(--shadow); padding: 18px 20px 14px; margin-bottom: 30px;
}
.mast-top { display: flex; align-items: center; gap: 16px; }
.mark { color: var(--red); flex: none; }
h1 {
  margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: clamp(25px, 6.2vw, 41px); font-weight: 700; letter-spacing: 0.055em;
  line-height: 1.04; text-transform: uppercase;
}
h1 .thin { display: block; font-size: 0.32em; letter-spacing: 0.34em;
           color: var(--muted); font-weight: 400; margin-bottom: 5px; }
.dateline {
  margin-top: 13px; padding-top: 10px; border-top: 2px solid var(--ink);
  display: flex; flex-wrap: wrap; gap: 7px 15px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted);
}
.dateline b { color: var(--ink); font-weight: 700; }

/* ---- section heads ----------------------------------------------------- */
h2 {
  display: flex; align-items: center; gap: 10px; margin: 40px 0 15px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; font-weight: 700; letter-spacing: 0.17em;
  text-transform: uppercase; color: var(--ink);
}
h2 .gl { color: var(--teal); flex: none; }
h2 .bar {
  flex: 1; height: 7px; border: 2px solid var(--ink); border-left: 0;
  background: repeating-linear-gradient(90deg,
    var(--ink) 0 4px, transparent 4px 8px);
}
h2 .n { flex: none; color: var(--muted); font-size: 11px; letter-spacing: 0.08em; }

/* ---- theme toggle ------------------------------------------------------ */
.tbtn {
  margin-left: auto; align-self: flex-start; cursor: pointer;
  display: flex; align-items: center; gap: 7px;
  background: var(--paper); color: var(--muted);
  border: 2px solid var(--ink); box-shadow: 3px 3px 0 var(--shadow);
  padding: 5px 9px; font-family: ui-monospace, Menlo, monospace;
  font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase;
}
.tbtn:hover { color: var(--ink); }
.tbtn:active { transform: translate(2px, 2px); box-shadow: 1px 1px 0 var(--shadow); }
.tbtn .tico { width: 11px; height: 11px; border: 2px solid currentColor;
              background: linear-gradient(90deg, currentColor 50%, transparent 50%); }

/* ---- filter bar -------------------------------------------------------- */
.controls {
  display: flex; flex-wrap: wrap; align-items: center; gap: 7px;
  margin: -14px 0 26px; padding: 11px 13px;
  background: var(--card); border: 2px dashed var(--rule);
}
.clabel { font-family: ui-monospace, Menlo, monospace; font-size: 10px;
          letter-spacing: 0.18em; color: var(--muted); margin-right: 3px; }
.fchip {
  cursor: pointer; font-family: ui-monospace, Menlo, monospace; font-size: 10.5px;
  letter-spacing: 0.04em; text-transform: uppercase;
  background: var(--paper); color: var(--muted);
  border: 1px solid var(--rule); padding: 4px 9px;
}
.fchip b { font-variant-numeric: tabular-nums; opacity: 0.75; }
.fchip.on { background: var(--ink); color: var(--card); border-color: var(--ink); }
.fchip:hover { border-color: var(--ink); }
.shown { margin-left: auto; font-family: ui-monospace, Menlo, monospace;
         font-size: 10.5px; color: var(--red); letter-spacing: 0.06em; }
.sec[hidden] { display: none; }

/* ---- item cards: library-card framing ---------------------------------- */
article {
  position: relative; background: var(--card);
  border: 2px solid var(--ink); box-shadow: 4px 4px 0 var(--shadow);
  padding: 15px 17px 14px; margin-bottom: 15px;
  display: flex; gap: 15px; align-items: flex-start;
  transition: transform .09s steps(2), box-shadow .09s steps(2);
}
.body { min-width: 0; flex: 1; }   /* min-width:0 or long titles refuse to wrap */
.cw { flex: none; border: 1px solid var(--rule); background: var(--paper);
      padding: 4px; line-height: 0; }
.cover .cv-bg { fill: var(--rule); }
.cover .cv-fg { fill: var(--red); }
article:nth-of-type(even) .cover .cv-fg { fill: var(--teal); }
article:hover { transform: translate(-2px, -2px); box-shadow: 6px 6px 0 var(--shadow); }
article.lead { border-width: 3px; box-shadow: 7px 7px 0 var(--shadow); padding: 20px 21px 18px; }
article.lead::before {
  content: "LEAD"; position: absolute; top: -2px; right: -2px;
  background: var(--red); color: var(--card); padding: 3px 9px;
  font-family: ui-monospace, Menlo, monospace; font-size: 10px;
  letter-spacing: 0.17em; font-weight: 700;
}
a.t {
  display: block; color: var(--ink); text-decoration: none;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 19px; line-height: 1.3; font-weight: 600; letter-spacing: -0.003em;
  padding-right: 54px;
}
article.lead a.t { font-size: 25px; line-height: 1.22; }
a.t:hover { color: var(--red); text-decoration: underline; text-underline-offset: 3px; }
/* the ruled line of an index card */
.rule { height: 0; border-top: 1px dashed var(--rule); margin: 11px 0 9px; }
.meta, .dest {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; letter-spacing: 0.03em; color: var(--muted);
}
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 9px; }
.meta .src { color: var(--inkSoft); font-weight: 700; }
.dest { margin-top: 5px; }
.dest .host { background: var(--paper); border: 1px solid var(--rule);
              padding: 1px 5px; color: var(--muted); }
.dest a { color: var(--teal); text-decoration: none; font-weight: 700; }
.dest a:hover { text-decoration: underline; }

/* ---- pixel score meter ------------------------------------------------- */
.meter { display: inline-flex; align-items: center; gap: 3px; margin-left: auto; }
.meter i { width: 7px; height: 11px; border: 1px solid var(--ink); display: block; }
.meter i.on { background: var(--gold); }
.meter i.off { background: transparent; opacity: 0.4; }
.meter b { font-size: 11px; color: var(--inkSoft); margin-left: 3px;
           font-variant-numeric: tabular-nums; }

.sum {
  margin-top: 10px; color: var(--inkSoft); font-size: 15px; line-height: 1.55;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
}
article.lead .sum { font-size: 16.5px; }
/* The brief. Labelled so a long abstract reads as a deliberate section rather
   than as the card running on; indented off a rule so the eye can skip it. */
.tldr {
  margin-top: 11px; padding-left: 11px; border-left: 3px solid var(--rule);
  color: var(--inkSoft); font-size: 14.5px; line-height: 1.58;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
}
.tldr .tl {
  display: inline-block; margin-right: 8px; vertical-align: 1px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 9.5px; letter-spacing: 0.15em; font-weight: 700;
  color: var(--card); background: var(--teal); padding: 2px 6px;
}
article.lead .tldr { font-size: 16px; border-left-color: var(--red); }
.chips { margin-top: 11px; display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px;
  letter-spacing: 0.05em; background: var(--paper); border: 1px solid var(--rule);
  color: var(--muted); padding: 2px 7px; white-space: nowrap;
}
.disc { margin-top: 8px; font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
.disc a { color: var(--red); font-weight: 700; text-decoration: none; }
.disc a:hover { text-decoration: underline; }

footer {
  margin-top: 46px; border-top: 3px double var(--ink); padding-top: 14px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; line-height: 1.75; color: var(--muted);
}
footer code { background: var(--card); border: 1px solid var(--rule); padding: 1px 5px; }
.empty {
  background: var(--card); border: 3px dashed var(--ink); padding: 34px 20px;
  text-align: center; color: var(--muted); box-shadow: 5px 5px 0 var(--shadow);
}
.empty .px { margin: 0 auto 14px; color: var(--rule); }

@media (max-width: 640px) {
  a.t { font-size: 17.5px; padding-right: 0; }
  article.lead a.t { font-size: 21px; }
  .mast-top { gap: 12px; }
  .meter { margin-left: 0; }
  /* Once the label wraps there is no room left for the rule, and a 4px
     remnant of it reads as a stray glyph beside the count. Drop to inline
     flow so the icon stays on the label's first line rather than being a
     flex item that wraps onto a line of its own. */
  h2 { display: block; line-height: 1.5; }
  h2 .gl { display: inline-block; vertical-align: -3px; margin-right: 8px; }
  h2 .bar { display: none; }
  h2 .n { display: inline; margin-left: 7px; }
  h2 .n::before { content: "— "; }
}
@media (prefers-reduced-motion: reduce) {
  article { transition: none; }
  article:hover { transform: none; }
}
"""


SCRIPT = """
(function () {
  'use strict';
  // Opened from file:// in some browsers, from a hosted URL in others.
  // Storage can throw outright on an opaque origin, so every access is
  // guarded and the page stays fully functional with none of it.
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };

  /* ---- theme: auto -> light -> dark --------------------------------- */
  var MODES = ['auto', 'light', 'dark'];
  var root = document.documentElement;
  var btn = document.getElementById('theme');
  var mode = store.get('rr-theme');
  if (MODES.indexOf(mode) === -1) { mode = 'auto'; }

  function applyTheme() {
    if (mode === 'auto') { root.removeAttribute('data-theme'); }
    else { root.setAttribute('data-theme', mode); }
    if (btn) { btn.querySelector('.tlbl').textContent = mode; }
  }
  applyTheme();
  if (btn) {
    btn.addEventListener('click', function () {
      mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      store.set('rr-theme', mode);
      applyTheme();
    });
  }

  /* ---- topic filter -------------------------------------------------- */
  var chips = [].slice.call(document.querySelectorAll('.fchip'));
  var sections = [].slice.call(document.querySelectorAll('.sec'));
  var shown = document.getElementById('shown');
  if (!chips.length) { return; }

  function activeTopics() {
    var on = {};
    chips.forEach(function (c) {
      if (c.dataset.topic !== '*' && c.classList.contains('on')) { on[c.dataset.topic] = 1; }
    });
    return on;
  }

  function render() {
    var on = activeTopics();
    var visible = 0, total = 0;
    sections.forEach(function (s) {
      var n = s.querySelectorAll('article').length;
      total += n;
      var show = on[s.dataset.topic];
      s.hidden = !show;
      if (show) { visible += n; }
    });
    var all = chips.filter(function (c) { return c.dataset.topic !== '*'; });
    var allOn = all.every(function (c) { return c.classList.contains('on'); });
    chips.forEach(function (c) {
      if (c.dataset.topic === '*') { c.classList.toggle('on', allOn); }
    });
    if (shown) {
      shown.textContent = visible === total ? '' : 'showing ' + visible + ' of ' + total;
    }
  }

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      if (chip.dataset.topic === '*') {
        // "all" is a reset, never a way to hide everything
        chips.forEach(function (c) { c.classList.add('on'); });
      } else {
        chip.classList.toggle('on');
        var any = chips.some(function (c) {
          return c.dataset.topic !== '*' && c.classList.contains('on');
        });
        // filtering down to an empty page is never what was meant
        if (!any) { chip.classList.add('on'); }
      }
      render();
    });
  });
  render();
})();
"""


LIBRARY_CSS = """
.controls.lib { gap: 9px; }
.controls.lib input[type=search] {
  flex: 1 1 230px; min-width: 0; font: inherit; font-size: 13px;
  background: var(--paper); color: var(--ink);
  border: 2px solid var(--ink); padding: 6px 9px;
}
.controls.lib label { font-family: ui-monospace, Menlo, monospace; font-size: 10px;
                      letter-spacing: 0.12em; text-transform: uppercase;
                      color: var(--muted); display: flex; align-items: center; gap: 5px; }
.controls.lib input[type=date], .controls.lib select {
  font: inherit; font-size: 11.5px; font-family: ui-monospace, Menlo, monospace;
  background: var(--paper); color: var(--ink);
  border: 1px solid var(--rule); padding: 4px 6px;
}
.controls.lib #clear { cursor: pointer; }
.morebtn { display: block; width: 100%; cursor: pointer; padding: 11px;
           margin-top: 4px; letter-spacing: 0.12em; }
#list article { margin-bottom: 9px; }
#list .meter b { margin-left: 0; }
.dateline a { color: var(--red); font-weight: 700; }
"""


def render_html(items, interests):
    labels = topic_labels(interests)
    live = len([t for t in interests["topics"] if t.get("enabled", True)])
    # Issue number from the day of the year — deterministic, needs no stored
    # counter, and gives the thing a sense of being a run of issues.
    issue = datetime.now().timetuple().tm_yday

    P = []
    P.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    P.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    P.append(f"<title>Reading Radar — {escape(TODAY)}</title>")
    P.append(f"<style>{CSS}</style></head><body><div class='wrap'>")

    P.append("<div class='mast'><div class='mast-top'>")
    P.append(f"<span class='mark'>{pixel_svg(RADAR_MARK, 4)}</span>")
    P.append("<h1><span class='thin'>the daily</span>Reading Radar</h1>")
    P.append("<button id='theme' class='tbtn' type='button' "
             "title='Theme: auto / light / dark' aria-label='Switch theme'>"
             "<span class='tico'></span><span class='tlbl'>auto</span></button>")
    P.append("</div><div class='dateline'>")
    P.append(f"<span><b>{escape(TODAY)}</b></span>")
    P.append(f"<span>No. <b>{issue}</b></span>")
    P.append(f"<span><b>{len(items)}</b> dispatch{'' if len(items) == 1 else 'es'}</span>")
    P.append(f"<span><b>{live}</b> topics on watch</span>")
    P.append("<span><a href='library.html'>the library →</a></span>")
    P.append("</div></div>")

    # Topic filter. Pure view-level: it hides cards that are already on the
    # page. It cannot change what gets fetched tomorrow — that lives in
    # interests.json, which a file:// page has no way to write.
    if items:
        counts = {}
        for topic, group_items in group(items, interests):
            counts[topic] = len(group_items)
        P.append("<div class='controls'><span class='clabel'>FILTER</span>")
        P.append("<button class='fchip on' data-topic='*' type='button'>"
                 f"all <b>{len(items)}</b></button>")
        for topic, n in counts.items():
            P.append(f"<button class='fchip on' data-topic='{escape(topic)}' type='button' "
                     f"title='{escape(labels.get(topic, topic))}'>"
                     f"{escape(short_label(topic))} <b>{n}</b></button>")
        P.append("<span class='shown' id='shown'></span></div>")

    if not items:
        P.append("<div class='empty'>"
                 + pixel_svg(PIXEL_GLYPHS["_default"], 7) +
                 "<p><b>Nothing cleared the bar today.</b></p>"
                 "<p>That's usually right — but if it keeps happening, run "
                 "<code>python3 radar.py --check-feeds</code> or lower "
                 "<code>DIGEST_MIN_SCORE</code> in config.py.</p></div>")

    first_section = True
    for topic, group_items in group(items, interests):
        P.append(f"<section class='sec' data-topic='{escape(topic)}'>")
        P.append(f"<h2><span class='gl'>{topic_glyph(topic)}</span>"
                 f"{escape(labels.get(topic, topic))}"
                 f"<span class='bar'></span><span class='n'>{len(group_items)}</span></h2>")
        for idx, it in enumerate(group_items):
            # Only the strongest item of the strongest section gets the lead
            # treatment — a page where everything shouts has no hierarchy.
            lead = first_section and idx == 0
            when = (it.get("published") or "")[:10]
            kind = KIND_LABEL.get(it.get("kind"), it.get("kind", ""))
            P.append("<article class='lead'>" if lead else "<article>")
            P.append("<div class='cw'>" + item_cover(it["url"], topic,
                                                     7 if lead else 5) + "</div>")
            P.append("<div class='body'>")
            # target=_blank matters more than usual here: the digest is opened
            # as a local file, so a same-tab navigation throws the whole page
            # away and there is no server to go "back" to.
            P.append(f"<a class='t' href='{escape(it['url'])}' target='_blank' "
                     f"rel='noopener noreferrer'>{escape(it['title'])}</a>")
            P.append("<div class='rule'></div>")
            bits = [f"<span class='src'>{escape(it['source'])}</span>"]
            if kind:
                bits.append(escape(kind))
            if when:
                bits.append(when)
            if it.get("also_in"):
                bits.append("also in " + escape(", ".join(sorted(set(it["also_in"])))[:60]))
            P.append("<div class='meta'>" + " · ".join(bits)
                     + score_meter(it["score"]) + "</div>")
            P.append(link_line(it))
            brief = tldr(it.get("summary"))
            if brief:
                P.append("<div class='tldr'><span class='tl'>TL;DR</span>"
                         f"<span>{escape(brief)}</span></div>")
            if it.get("extra", {}).get("discussion"):
                P.append(f"<div class='disc'><a href='{escape(it['extra']['discussion'])}' "
                         f"target='_blank' rel='noopener noreferrer'>"
                         f"▸ HN discussion ({it['extra'].get('points', '?')} points)</a></div>")
            if it.get("hits"):
                P.append("<div class='chips'>"
                         + "".join(f"<span class='chip'>{escape(h)}</span>" for h in it["hits"][:7])
                         + "</div>")
            P.append("</div></article>")
        P.append("</section>")
        first_section = False

    P.append("<footer>"
             "─── END OF ISSUE ───<br>"
             "Set in pixels by <code>work/reading-radar</code>. "
             "Too much noise, or not enough? Edit <code>interests.json</code> — "
             "weights there drive both what gets fetched and what scores well."
             "</footer>")
    P.append(f"<script>{SCRIPT}</script>")
    P.append("</div></body></html>")
    return "\n".join(P)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(html, md, count, env):
    need = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        print(f"! email skipped — missing in .env: {', '.join(missing)}", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = f"Reading Radar — {TODAY} ({count} items)"
    msg["From"] = env.get("DIGEST_FROM", env["SMTP_USER"])
    msg["To"] = env["DIGEST_TO"]
    msg.set_content(md)                 # plain-text fallback
    msg.add_alternative(html, subtype="html")

    port = int(env.get("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, context=ctx, timeout=30) as s:
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.send_message(msg)
    else:                               # 587 and friends: upgrade in place
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(env["SMTP_USER"], env["SMTP_PASS"])
            s.send_message(msg)
    print(f"emailed {count} items to {env['DIGEST_TO']}")
    return True


LIBRARY_SCRIPT = """
(function () {
  'use strict';
  var ROWS = window.__RR_ROWS__ || [];
  var PAGE = 60;
  var shown = PAGE;

  var q = document.getElementById('q');
  var from = document.getElementById('from');
  var to = document.getElementById('to');
  var topicSel = document.getElementById('topic');
  var list = document.getElementById('list');
  var stat = document.getElementById('stat');
  var more = document.getElementById('more');

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function matches() {
    var text = (q.value || '').toLowerCase().trim();
    var words = text ? text.split(/\\s+/) : [];
    var f = from.value, t = to.value, tp = topicSel.value;
    return ROWS.filter(function (r) {
      if (f && r.d < f) { return false; }
      if (t && r.d > t) { return false; }
      if (tp && (r.t || []).indexOf(tp) === -1) { return false; }
      if (!words.length) { return true; }
      var hay = (r.n + ' ' + r.s).toLowerCase();
      // every word must appear, so extra words narrow rather than widen
      return words.every(function (w) { return hay.indexOf(w) !== -1; });
    });
  }

  function render() {
    var rows = matches();
    stat.textContent = rows.length + ' of ' + ROWS.length +
      (ROWS.length === 1 ? ' entry' : ' entries');
    var slice = rows.slice(0, shown);
    list.innerHTML = slice.map(function (r) {
      return '<article><div class="body">' +
        '<a class="t" href="' + esc(r.u) + '" target="_blank" rel="noopener noreferrer">' +
        esc(r.n) + '</a>' +
        '<div class="rule"></div>' +
        '<div class="meta"><span class="src">' + esc(r.s) + '</span> · ' + esc(r.d) +
        '<span class="meter"><b>' + esc(r.c) + '</b></span></div>' +
        '</div></article>';
    }).join('');
    more.hidden = rows.length <= shown;
    more.textContent = 'show ' + Math.min(PAGE, rows.length - shown) + ' more';
  }

  function reset() { shown = PAGE; render(); }
  [q, from, to, topicSel].forEach(function (el) {
    el.addEventListener('input', reset);
    el.addEventListener('change', reset);
  });
  more.addEventListener('click', function () { shown += PAGE; render(); });
  document.getElementById('clear').addEventListener('click', function () {
    q.value = ''; from.value = ''; to.value = ''; topicSel.value = '';
    reset();
  });
  render();
})();
"""


def render_library(rows, interests):
    """Everything ever surfaced, searchable.

    All rows are embedded as compact JSON and the filtering happens in the
    browser, because a file:// page cannot fetch a sibling file — Chrome blocks
    it. Only a page of results is ever put into the DOM: the data is small
    enough to hold in memory for years, but rendering thousands of cards is not.
    """
    labels = topic_labels(interests)
    rows = sorted(rows, key=lambda r: (r.get("first_seen", ""), r.get("score", 0)),
                  reverse=True)
    # short keys — this is the payload, and it repeats per row
    compact = [{"d": r.get("first_seen", ""), "u": r.get("url", ""),
                "n": r.get("title", ""), "s": r.get("source", ""),
                "c": r.get("score", 0), "t": r.get("topics", [])} for r in rows]
    seen_topics = sorted({t for r in rows for t in r.get("topics", [])})
    dates = [r.get("first_seen", "") for r in rows if r.get("first_seen")]

    P = []
    P.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    P.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    P.append("<title>Reading Radar — Library</title>")
    P.append(f"<style>{CSS}{LIBRARY_CSS}</style></head><body><div class='wrap'>")
    P.append("<div class='mast'><div class='mast-top'>")
    P.append(f"<span class='mark'>{pixel_svg(RADAR_MARK, 4)}</span>")
    P.append("<h1><span class='thin'>the</span>Library</h1>")
    P.append("<button id='theme' class='tbtn' type='button' aria-label='Switch theme'>"
             "<span class='tico'></span><span class='tlbl'>auto</span></button>")
    P.append("</div><div class='dateline'>")
    P.append(f"<span><b>{len(rows)}</b> entries</span>")
    if dates:
        P.append(f"<span><b>{min(dates)}</b> → <b>{max(dates)}</b></span>")
    P.append("<span><a href='digest.html'>← today's issue</a></span>")
    P.append("</div></div>")

    P.append("<div class='controls lib'>")
    P.append("<input id='q' type='search' placeholder='search titles and sources…' "
             "autocomplete='off'>")
    P.append("<label>from <input id='from' type='date'></label>")
    P.append("<label>to <input id='to' type='date'></label>")
    P.append("<select id='topic'><option value=''>all topics</option>")
    for t in seen_topics:
        P.append(f"<option value='{escape(t)}'>{escape(labels.get(t, t))}</option>")
    P.append("</select>")
    P.append("<button id='clear' class='fchip' type='button'>clear</button>")
    P.append("<span class='shown' id='stat'></span>")
    P.append("</div>")

    P.append("<div id='list'></div>")
    P.append("<button id='more' class='fchip morebtn' type='button' hidden></button>")
    P.append("<footer>─── THE WHOLE RUN ───<br>"
             "Every item the radar has surfaced, oldest kept forever. "
             f"Source of truth is <code>{ARCHIVE}</code>, which is gitignored.</footer>")
    P.append("<script>window.__RR_ROWS__=" +
             json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + ";</script>")
    P.append(f"<script>{SCRIPT}</script>")
    P.append(f"<script>{LIBRARY_SCRIPT}</script>")
    P.append("</div></body></html>")
    return "\n".join(P)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", action="store_true", help="send the digest (needs .env)")
    ap.add_argument("--open", action="store_true", dest="open_browser",
                    help="open digest.html when done")
    args = ap.parse_args()

    items_path = HERE / "items.json"
    if not items_path.exists():
        raise SystemExit("no items.json — run radar.py first")
    items = json.loads(items_path.read_text())
    interests = json.loads((HERE / "interests.json").read_text())

    # Archive before rendering, so today's issue is already in the library.
    added = append_archive(items)
    archive_rows = load_archive()

    html, md = render_html(items, interests), render_md(items, interests)
    (HERE / "digest.html").write_text(html, encoding="utf-8")
    (HERE / "digest.md").write_text(md, encoding="utf-8")
    (HERE / "library.html").write_text(render_library(archive_rows, interests),
                                       encoding="utf-8")
    print(f"wrote digest.html and digest.md ({len(items)} items)")
    print(f"archived {added} new ({len(archive_rows)} total) -> library.html")

    if args.email:
        send_email(html, md, len(items), load_env())
    if args.open_browser:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(HERE / "digest.html")], check=False)


if __name__ == "__main__":
    main()
