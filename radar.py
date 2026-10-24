#!/usr/bin/env python3
"""Reading Radar — fetch today's candidates, score them against interests.json.

Sweeps four kinds of source (curated RSS, arXiv by category, Hacker News by
keyword, Google News by keyword), scores everything against the topic profile,
drops anything that matches no topic, and writes items.json for digest.py.

Stdlib only, on purpose: a cron job that needs `pip install` to run is a cron
job that silently stops running after the next OS upgrade.

  python3 radar.py              # normal daily run
  python3 radar.py --dry-run    # fetch + score, print, write nothing
  python3 radar.py --since 168  # catch up after a week away
"""
import argparse
import gzip
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config

NOW = datetime.now(timezone.utc)
ATOM = "{http://www.w3.org/2005/Atom}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


def local(tag):
    """Tag name without its namespace. '{...}item' -> 'item'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(el, *names):
    """First child's text matching any of `names` by local name.

    Namespace-blind on purpose: the same logical field is `title` in RSS 2.0,
    `{http://purl.org/rss/1.0/}title` in RSS 1.0, and the date arrives as
    `pubDate`, `dc:date` or `date` depending on the publisher.
    """
    for child in el:
        if local(child.tag) in names and child.text and child.text.strip():
            return child.text
    return None


TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|oc|ref)")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# HTTP + parsing
# ---------------------------------------------------------------------------

class _Redirect308(urllib.request.HTTPRedirectHandler):
    """Follow 308 Permanent Redirect.

    urllib only learned 308 in Python 3.11; this Mac runs 3.9, where a
    308 raises HTTPError and the feed looks dead. deeplearning.ai's The Batch
    is served that way.

    Both halves are needed: http_error_308 to dispatch at all, and
    redirect_request because 3.9's version whitelists {301,302,303,307} and
    re-raises anything else even once dispatched. 308 is 307 plus permanence,
    so it maps to 307 — which preserves the GET.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(
            req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_301


_opener = urllib.request.build_opener(_Redirect308)


def fetch(url):
    """GET a URL, returning bytes or None. Never raises — one dead feed must
    not take the whole digest down with it."""
    req = urllib.request.Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/json, text/xml, */*",
    })
    try:
        with _opener.open(req, timeout=config.HTTP_TIMEOUT) as r:
            body = r.read()
            encoding = (r.headers.get("Content-Encoding") or "").lower()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"  ! {url[:70]} -> {exc}", file=sys.stderr)
        return None

    # Some CDNs return gzip whether or not it was requested — deepmind.google
    # does it intermittently. Undecompressed, the feed parses to zero items and
    # looks like a dead URL, so the source vanishes from the digest silently.
    # Sniff the magic bytes as well as the header: servers mislabel this.
    try:
        if encoding == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error) as exc:
        print(f"  ! {url[:70]} -> could not decompress ({exc})", file=sys.stderr)
        return None
    return body


def clean(text):
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", text or ""))).strip()


def parse_date(raw):
    """RSS uses RFC-822 dates, Atom uses ISO-8601. Accept either."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        d = parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_feed(blob):
    """Parse RSS 2.0, RSS 1.0/RDF or Atom into [(title, link, published, summary)]."""
    if not blob:
        return []
    try:
        root = ET.fromstring(blob)
    except ET.ParseError as exc:
        print(f"  ! unparseable feed: {exc}", file=sys.stderr)
        return []

    out = []
    # RSS 2.0 and RSS 1.0/RDF. Matched on local name because RSS 1.0 puts every
    # element in the http://purl.org/rss/1.0/ namespace while RSS 2.0 namespaces
    # none of them — iter("item") finds only the latter, which silently yielded
    # zero items for Nature, Science, bioRxiv and Nature Biotechnology.
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        link = (child_text(item, "link") or "").strip()
        if not link:                                   # RDF puts it in an attribute
            link = (item.get(RDF + "about") or "").strip()
        out.append((clean(child_text(item, "title")), link,
                    parse_date(child_text(item, "pubDate", "date")),
                    clean(child_text(item, "description", "summary"))))
    # Atom
    for entry in root.iter(ATOM + "entry"):
        link = ""
        for ln in entry.findall(ATOM + "link"):
            if ln.get("rel") in (None, "alternate"):
                link = ln.get("href") or ""
                break
        out.append((clean(entry.findtext(ATOM + "title")), link.strip(),
                    parse_date(entry.findtext(ATOM + "published")
                               or entry.findtext(ATOM + "updated")),
                    clean(entry.findtext(ATOM + "summary")
                          or entry.findtext(ATOM + "content"))))
    return [(t, l, d, s) for t, l, d, s in out if t and l]


def feed_sources(blob):
    """Map item link -> (publisher name, publisher homepage) from <source url>.

    Google News hides the real article behind a news.google.com redirect whose
    id is an opaque Google token — not a decodable URL — and the redirect itself
    serves a JavaScript interstitial, so the destination cannot be resolved
    without a headless browser. What the feed *does* give is
    `<source url="https://publisher.com">Publisher</source>`, which is enough to
    say who published a piece and link to them directly.

    Returned separately rather than widening parse_feed's tuple, which three
    other call sites unpack.
    """
    if not blob:
        return {}
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return {}
    out = {}
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        link = (child_text(item, "link") or "").strip()
        if not link:
            continue
        for child in item:
            if local(child.tag) == "source":
                name = (child.text or "").strip()
                href = (child.get("url") or "").strip()
                if name or href:
                    out[link] = (name, href)
                break
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

# Publishing-platform furniture that rides along in RSS and reads as content.
# Stripped at ingest so items.json, the digest and the Markdown all stay clean.
_TITLE_CRUFT = re.compile(r"\s+via\s+@[\w.]+(?:\s*,\s*@[\w.]+)*\s*$", re.I)
_SUMMARY_CRUFT = [
    # WordPress/Yoast: "The post <title> appeared first on <Publisher>."
    re.compile(r"\s*The post\s+.*?\s+appeared first on\s+[^.]*\.?\s*$", re.I | re.S),
    re.compile(r"\s*(Continue reading|Read more|The article)\b.*$", re.I | re.S),
    re.compile(r"\s*\[\s*…?\s*\]\s*$"),
]


def strip_boilerplate(title, summary):
    title = _TITLE_CRUFT.sub("", title or "").strip()
    summary = (summary or "").strip()
    for rx in _SUMMARY_CRUFT:
        summary = rx.sub("", summary).strip()
    # A summary that is just the headline again adds nothing but height.
    if summary and summary.lower().startswith(title.lower()[:60]):
        summary = summary[len(title):].lstrip(" .—–-")
    return title, summary


def from_rss(spec, cutoff):
    name, url, weight = spec
    items = []
    for title, link, pub, summary in parse_feed(fetch(url)):
        if pub and pub < cutoff:
            continue
        title, summary = strip_boilerplate(title, summary)
        if not title:
            continue
        items.append({"title": title, "url": link, "summary": summary[:1400],
                      "published": pub, "source": name, "kind": "blog",
                      "source_weight": weight, "extra": {}})
    return items


def from_arxiv(category, cutoff):
    q = urllib.parse.urlencode({
        "search_query": f"cat:{category}",
        "start": 0,
        "max_results": config.ARXIV_MAX_PER_CATEGORY,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    items = []
    for title, link, pub, summary in parse_feed(fetch(f"http://export.arxiv.org/api/query?{q}")):
        if pub and pub < cutoff:
            continue
        items.append({"title": title, "url": link, "summary": summary[:1400],
                      "published": pub, "source": "arXiv", "kind": "paper",
                      "source_weight": 1.25, "extra": {"category": category}})
    return items


def from_hn(query, cutoff):
    url = ("https://hn.algolia.com/api/v1/search_by_date?"
           + urllib.parse.urlencode({
               "query": query, "tags": "story", "hitsPerPage": 30,
               "numericFilters": f"created_at_i>{int(cutoff.timestamp())}",
           }))
    blob = fetch(url)
    if not blob:
        return []
    try:
        hits = json.loads(blob).get("hits", [])
    except json.JSONDecodeError:
        return []
    items = []
    for h in hits:
        points = h.get("points") or 0
        if points < config.HN_MIN_POINTS:
            continue
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        items.append({
            "title": clean(h.get("title")), "url": link,
            "summary": clean(h.get("story_text"))[:600],
            "published": parse_date(h.get("created_at")),
            "source": "Hacker News", "kind": "discussion",
            # A 400-point story is better vetted than a 45-point one.
            "source_weight": 0.9 + min(0.4, points / 1000.0),
            "extra": {"points": points,
                      "discussion": f"https://news.ycombinator.com/item?id={h.get('objectID')}"},
        })
    return items


def from_google_news(query, cutoff):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(f"{query} when:3d") + "&" + config.GOOGLE_NEWS_LOCALE)
    blob = fetch(url)
    sources = feed_sources(blob)
    items = []
    for title, link, pub, summary in parse_feed(blob):
        if pub and pub < cutoff:
            continue
        # Google appends " - Publisher" to every headline; keep it as the source.
        publisher = title.rsplit(" - ", 1)[-1] if " - " in title else "Google News"
        clean_title = title.rsplit(" - ", 1)[0] if " - " in title else title
        src_name, src_url = sources.get(link, ("", ""))

        # Google's <description> is just an anchor repeating the headline plus
        # the publisher name, so it renders as the title twice. Drop it when it
        # carries nothing the title does not already say.
        stripped = re.sub(r"\W+", " ", summary or "").strip().lower()
        title_words = re.sub(r"\W+", " ", clean_title).strip().lower()
        if not stripped or stripped.startswith(title_words[:60]):
            summary = ""

        items.append({"title": clean_title,
                      "url": link, "summary": summary[:1400], "published": pub,
                      "source": f"News · {src_name or publisher}", "kind": "news",
                      "source_weight": 0.85,
                      # The link above is a Google redirect. Carry the publisher
                      # so the digest can say where it actually goes and offer a
                      # direct link to them.
                      "extra": {"publisher": src_name or publisher,
                                "publisher_url": src_url} if src_url else {}})
    return items


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

kw_pattern = config.kw_pattern   # shared with history_profile.py; see config.py


def score_item(item, interests):
    text = f"{item['title']} . {item['summary']}"

    for term in interests.get("mute", []):
        if kw_pattern(term).search(text):
            return 0.0, [], ["muted: " + term]

    total, topics, hits = 0.0, [], []
    for topic in interests["topics"]:
        if not topic.get("enabled", True) or topic.get("weight", 0) <= 0:
            continue
        if any(kw_pattern(m).search(text) for m in topic.get("mute", [])):
            continue
        # Optional gate for topics whose include list contains words that are
        # only domain-specific in context. 'agent', 'benchmark' and 'evaluation'
        # are LLM terms in an LLM article and ordinary English everywhere else:
        # a bioRxiv paper on "benchmarking fluorescent imaging agents" matched
        # llm_agents on four keywords and landed mid-digest at weight 3.0.
        # Topics without require_any behave exactly as before.
        require = topic.get("require_any")
        if require and not any(kw_pattern(r).search(text) for r in require):
            continue
        matched = [kw for kw in topic["include"] if kw_pattern(kw).search(text)]
        if not matched:
            continue
        # Density bonus, flattened: 4 keyword hits is better evidence than 1,
        # but not 4x better — past ~3 it's the same article.
        sub = topic["weight"] * (1.0 + 0.3 * min(3, len(matched) - 1))
        boosted = [kw for kw in topic.get("boost", []) if kw_pattern(kw).search(text)]
        sub += 0.4 * topic["weight"] * min(2, len(boosted))
        total += sub
        topics.append(topic["name"])
        hits += matched[:4] + boosted[:2]

    if not topics:                      # matches nothing she reads -> not her problem
        return 0.0, [], []

    total *= item["source_weight"]
    if item["source"] in interests.get("prefer_sources", []):
        total *= 1.15
    if item["source"] in interests.get("demote_sources", []):
        total *= 0.75

    # Recency decay, linear and gentle — a great paper from yesterday still beats
    # a mediocre blog post from an hour ago.
    if item["published"]:
        age_h = max(0.0, (NOW - item["published"]).total_seconds() / 3600.0)
        total *= max(0.5, 1.0 - 0.012 * age_h)

    return round(total, 2), topics, sorted(set(hits))


def primary_topic(item, interests):
    """The item's strongest topic — the same rule the digest groups by."""
    order = [t["name"] for t in sorted(interests["topics"],
                                       key=lambda t: -t.get("weight", 0))]
    rank = {n: i for i, n in enumerate(order)}
    tops = item.get("topics") or []
    return min(tops, key=lambda t: rank.get(t, 99)) if tops else "other"


def select(items, interests):
    """Choose the digest in two passes: reserved slots first, then by score.

    A single global top-N ranking lets the heaviest topic crowd everything
    else out. With llm_agents at weight 3.0 producing twenty items scoring
    3-5, a weight-1.5 biotech item at 2.0 never made the cut — the science
    topics were enabled and scoring and still produced nothing for days,
    which reads as "the feeds are broken" rather than "the ranking is working
    as designed".

    So each enabled topic first gets up to RESERVED_PER_TOPIC of its own best
    items, and only then is the remainder filled by global score. Items still
    have to clear DIGEST_MIN_SCORE to be here at all, so this widens the range
    of what appears without lowering the bar for what qualifies.
    """
    by_score = sorted(items, key=lambda i: -i["score"])
    per_source, chosen, taken = {}, [], set()
    cap = config.DIGEST_MAX_ITEMS

    def take(it):
        """Respect the per-source cap in both passes, not just the second."""
        n = per_source.get(it["source"], 0)
        if n >= config.MAX_PER_SOURCE or id(it) in taken:
            return False
        per_source[it["source"]] = n + 1
        taken.add(id(it))
        chosen.append(it)
        return True

    # Pass 1 — reserved slots, strongest topics first so that if the digest is
    # small the heavy topics still lead.
    for topic in [t["name"] for t in sorted(interests["topics"],
                                            key=lambda t: -t.get("weight", 0))
                  if t.get("enabled", True)]:
        got = 0
        for it in by_score:
            if got >= config.RESERVED_PER_TOPIC or len(chosen) >= cap:
                break
            if primary_topic(it, interests) == topic and take(it):
                got += 1

    # Pass 2 — fill whatever is left purely by score.
    for it in by_score:
        if len(chosen) >= cap:
            break
        take(it)

    return sorted(chosen, key=lambda i: -i["score"])


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def canonical(url):
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return url
    keep = [kv for kv in p.query.split("&") if kv and not TRACKING.match(kv.split("=")[0])]
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}" + (("?" + "&".join(sorted(keep))) if keep else "")


def title_key(title):
    return frozenset(w for w in re.findall(r"[a-z0-9]{4,}", title.lower()))


def dedupe(items):
    """Collapse the same story arriving from several sources.

    Exact match on canonical URL, plus Jaccard >= 0.75 on title words, which
    catches the same paper on arXiv and HN, or one headline across five outlets.
    """
    by_url, kept, merged = {}, [], 0
    for it in sorted(items, key=lambda i: -i["score"]):
        key = canonical(it["url"])
        if key in by_url:
            merged += 1
            continue
        by_url[key] = it
        tk = title_key(it["title"])
        dup = False
        for other in kept:
            ok = title_key(other["title"])
            union = tk | ok
            if union and len(tk & ok) / len(union) >= 0.75:
                other.setdefault("also_in", []).append(it["source"])
                dup, merged = True, merged + 1
                break
        if not dup:
            kept.append(it)
    return kept, merged


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def load_seen(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen(path, seen, new_keys):
    horizon = NOW - timedelta(days=config.SEEN_RETENTION_DAYS)
    pruned = {k: v for k, v in seen.items()
              if (parse_date(v) or NOW) >= horizon}
    for k in new_keys:
        pruned[k] = NOW.isoformat()
    path.write_text(json.dumps(pruned, indent=0, sort_keys=True))



def check_feeds():
    """Probe every configured feed and report which ones are actually alive.

    Feed URLs rot — sites move to a new CMS and silently drop /feed/. Run this
    after editing RSS_FEEDS, and any time the digest looks thin.
    """
    print(f"checking {len(config.RSS_FEEDS)} feeds...\n")
    dead = []
    def probe(spec):
        name, url, _ = spec
        blob = fetch(url)
        if not blob:
            return name, url, "UNREACHABLE", 0
        items = parse_feed(blob)
        if not items:
            # Distinguish a dead URL from a live one we can't read: the first
            # time this ran, four healthy RSS 1.0 feeds and one gzipped one
            # reported "NO ITEMS" and were nearly deleted as stale URLs.
            return name, url, (f"FETCHED {len(blob):,} bytes but parsed 0 items "
                               "— wrong format, or a block/error page?"), 0
        newest = max((d for _, _, d, _ in items if d), default=None)
        age = f"newest {(NOW - newest).days}d old" if newest else "no dates"
        return name, url, f"ok, {len(items)} items, {age}", len(items)

    with ThreadPoolExecutor(max_workers=config.HTTP_WORKERS) as pool:
        for name, url, status, n in pool.map(probe, config.RSS_FEEDS):
            flag = "  " if n else "!!"
            print(f"{flag} {name:26} {status}")
            if not n:
                dead.append((name, url))
    if dead:
        print(f"\n{len(dead)} feed(s) need fixing in config.py RSS_FEEDS:")
        for name, url in dead:
            print(f"  - {name}: {url}")
        print("Find the current URL (view-source on the site, look for "
              "application/rss+xml) or drop the line.")
    else:
        print("\nall feeds alive")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=config.LOOKBACK_HOURS,
                    help=f"lookback hours for news/blogs (default {config.LOOKBACK_HOURS})")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ap.add_argument("--include-seen", action="store_true",
                    help="don't filter out items from previous digests")
    ap.add_argument("--check-feeds", action="store_true",
                    help="probe every feed in config.py and report dead ones, then exit")
    args = ap.parse_args()

    if args.check_feeds:
        check_feeds()
        return

    interests = json.loads((HERE / "interests.json").read_text())
    cutoff = NOW - timedelta(hours=args.since)
    arxiv_cutoff = NOW - timedelta(hours=max(args.since, config.ARXIV_LOOKBACK_HOURS))

    # Topic queries drive the keyword-based sources, so the profile is the one
    # place that decides both what gets fetched and what scores well.
    queries = []
    for t in interests["topics"]:
        if t.get("enabled", True) and t.get("weight", 0) > 0:
            queries += t.get("queries", [])
    queries = list(dict.fromkeys(queries))

    jobs = ([(from_rss, spec, cutoff) for spec in config.RSS_FEEDS]
            + [(from_arxiv, cat, arxiv_cutoff) for cat in config.ARXIV_CATEGORIES]
            + [(from_hn, q, cutoff) for q in queries]
            + [(from_google_news, q, cutoff) for q in queries])

    print(f"fetching {len(jobs)} sources ({len(config.RSS_FEEDS)} feeds, "
          f"{len(config.ARXIV_CATEGORIES)} arXiv categories, {len(queries)} queries x2)...")
    raw = []
    with ThreadPoolExecutor(max_workers=config.HTTP_WORKERS) as pool:
        for got in pool.map(lambda j: j[0](j[1], j[2]), jobs):
            raw += got
    print(f"  {len(raw)} raw items")

    scored = []
    for it in raw:
        it["score"], it["topics"], it["hits"] = score_item(it, interests)
        if it["score"] >= config.DIGEST_MIN_SCORE:
            scored.append(it)
    print(f"  {len(scored)} above score {config.DIGEST_MIN_SCORE} "
          f"({len(raw) - len(scored)} dropped as off-topic or muted)")

    deduped, merged = dedupe(scored)
    print(f"  {len(deduped)} after dedupe ({merged} duplicates merged)")

    seen_path = HERE / "seen.json"
    seen = load_seen(seen_path)
    if not args.include_seen:
        before = len(deduped)
        deduped = [i for i in deduped if canonical(i["url"]) not in seen]
        print(f"  {len(deduped)} new ({before - len(deduped)} already sent in a past digest)")

    final = select(deduped, interests)

    for it in final:
        it["published"] = it["published"].isoformat() if it["published"] else None

    print(f"\n=== digest: {len(final)} items ===")
    for it in final:
        when = (it["published"] or "")[:10]
        print(f"{it['score']:6.2f}  [{','.join(it['topics'])[:28]:28}] {it['source'][:22]:22} "
              f"{when}  {it['title'][:70]}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    (HERE / "items.json").write_text(json.dumps(final, indent=2, ensure_ascii=False))
    save_seen(seen_path, seen, [canonical(i["url"]) for i in final])
    print(f"\nwrote items.json ({len(final)} items), updated seen.json")


if __name__ == "__main__":
    main()
