#!/usr/bin/env python3
"""Derive an interest profile from real browsing history.

Reads either a Chrome `History` SQLite file or a Google Takeout "My Activity"
export, strips out everything that isn't deliberate reading, counts what's
left, and sorts the surviving terms into the topic taxonomy in config.py.

Writes TWO files, both gitignored:
  interests.derived.json  — a PROPOSAL, for merging into interests.json by hand
  history_report.md       — what it found, and what it threw away, with counts

It never edits interests.json. A profile that silently rewrites itself from
noisy input is worse than one that's slightly stale, because you can't tell
when it drifted.

Usage
-----
  # Chrome's own database (quit Chrome first, or it's locked)
  python3 history_profile.py --chrome-history ~/Library/Application\\ Support/Google/Chrome/Default/History

  # Google Takeout export (a file, or the unzipped folder)
  python3 history_profile.py --takeout ~/Downloads/Takeout/My\\ Activity/Chrome/MyActivity.json
  python3 history_profile.py --takeout ~/Downloads/Takeout

  # Both at once, last 6 months only
  python3 history_profile.py --chrome-history ... --takeout ... --days 180
"""
import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config

CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
NOW = datetime.now(timezone.utc)

# Tracking params that make two identical pages look like two different URLs.
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|igshid|_hsenc)")
SEARCH_HOSTS = ("google.com", "google.co.in", "bing.com", "duckduckgo.com", "perplexity.ai")
WORD_RE = re.compile(r"[a-z][a-z0-9+#.\-]{1,}")
PERSONAL_RES = [re.compile(p, re.I) for p in config.PERSONAL_PATTERNS]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_chrome(path, cutoff):
    """Read urls table from a Chrome History SQLite file.

    Chrome keeps the live DB locked, so work on a copy. Timestamps are
    microseconds since 1601-01-01 (the WebKit epoch), not Unix seconds.
    """
    src = Path(path).expanduser()
    if not src.exists():
        raise SystemExit(
            f"no such file: {src}\n"
            "Check the profile folder — it may be 'Profile 1', 'Profile 2', ... "
            "rather than 'Default'. List them with:\n"
            "  ls ~/Library/Application\\ Support/Google/Chrome/")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        try:
            shutil.copy2(src, copy)      # also sidesteps the "database is locked" error
            # Recent visits may still be sitting in a write-ahead-log sidecar.
            # Copying History alone would silently lose the last few days.
            for suffix in ("-wal", "-shm"):
                side = src.with_name(src.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
        except PermissionError:
            raise SystemExit(
                f"macOS blocked reading {src}\n\n"
                "Your terminal needs Full Disk Access:\n"
                "  System Settings > Privacy & Security > Full Disk Access\n"
                "  > + > add Terminal (or iTerm), then QUIT and reopen it.\n\n"
                "Alternatively, copy the file somewhere readable first:\n"
                "  cp ~/Library/Application\\ Support/Google/Chrome/Default/History /tmp/chrome-history\n"
                "  python3 history_profile.py --chrome-history /tmp/chrome-history")
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT url, title, visit_count, last_visit_time FROM urls"
            ).fetchall()
            con.close()
        except sqlite3.DatabaseError as exc:
            raise SystemExit(
                f"could not read {src} as a Chrome history database ({exc}).\n"
                "Quit Chrome and try again, or use the Takeout route instead.")

    out = []
    for url, title, visits, ts in rows:
        when = None
        if ts:
            try:
                when = CHROME_EPOCH + timedelta(microseconds=int(ts))
            except (ValueError, OverflowError):
                when = None
        if when and cutoff and when < cutoff:
            continue
        out.append({"url": url or "", "title": title or "", "visits": max(1, visits or 1),
                    "when": when, "src": "chrome"})
    return out, len(rows)


def load_takeout(path, cutoff):
    """Read Google Takeout 'My Activity' exports — JSON or HTML, file or folder."""
    root = Path(path).expanduser()
    if not root.exists():
        raise SystemExit(f"no such path: {root}")
    files = []
    if root.is_dir():
        files = [p for p in root.rglob("*") if p.suffix.lower() in (".json", ".html")
                 and "activity" in p.name.lower()]
        if not files:  # some exports name it differently; fall back to anything plausible
            files = [p for p in root.rglob("*.json")] + [p for p in root.rglob("*.html")]
    else:
        files = [root]

    out, raw = [], 0
    for f in files:
        try:
            if f.suffix.lower() == ".json":
                entries = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                if not isinstance(entries, list):
                    continue
                raw += len(entries)
                out.extend(_takeout_json(entries, cutoff))
            else:
                text = f.read_text(encoding="utf-8", errors="replace")
                got, n = _takeout_html(text, cutoff)
                raw += n
                out.extend(got)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ! skipped {f.name}: {exc}", file=sys.stderr)
    return out, raw


def _takeout_json(entries, cutoff):
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        title = (e.get("title") or "").strip()
        url = (e.get("titleUrl") or "").strip()
        when = None
        if e.get("time"):
            try:
                when = datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
            except ValueError:
                when = None
        if when and cutoff and when < cutoff:
            continue
        # Titles arrive as "Visited <page>" / "Searched for <query>".
        for prefix in ("Visited ", "Searched for ", "Viewed ", "Used Search for "):
            if title.startswith(prefix):
                title = title[len(prefix):]
                break
        out.append({"url": url, "title": title, "visits": 1, "when": when, "src": "takeout"})
    return out


def _takeout_html(text, cutoff):
    """Tolerant regex parse of the HTML flavour of My Activity.

    The markup is one <a href>title</a> per activity followed by a date string.
    Parsing it properly would need the full DOM; this gets the two fields that
    matter and is honest about dates it can't read (keeps them).
    """
    pat = re.compile(
        r"(?:Visited|Searched for|Viewed)\s*<a href=\"([^\"]+)\"[^>]*>(.*?)</a>(.{0,60})",
        re.I | re.S)
    out, n = [], 0
    for m in pat.finditer(text):
        n += 1
        url, title, tail = m.group(1), unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), m.group(3)
        when = None
        dm = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", tail)
        if dm:
            try:
                when = datetime.strptime(dm.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                when = None
        if when and cutoff and when < cutoff:
            continue
        out.append({"url": url, "title": title, "visits": 1, "when": when, "src": "takeout"})
    return out, n


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def registered_domain(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def canonical(url):
    """Strip fragments and tracking params so reloads collapse to one row."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    keep = [kv for kv in p.query.split("&")
            if kv and not TRACKING_PARAMS.match(kv.split("=")[0])]
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{(p.hostname or '').lower()}{path}" + (("?" + "&".join(sorted(keep))) if keep else "")


def is_noise_domain(dom):
    if any(dom == d or dom.endswith("." + d) for d in config.NOISE_DOMAINS):
        return True
    # careers.adobe.com, jobs.ebayinc.com, ... — a listing on any employer's
    # own site, without enumerating employers.
    return dom.split(".")[0] in config.NOISE_SUBDOMAIN_PREFIXES


def search_query(url, title):
    """Return the query string if this row is a search, else None.

    Exact host match, not a suffix match: `docs.google.com` ends with
    `google.com` too, and a suffix test routed every Docs, Drive, Calendar and
    accounts page down the search path — where the noise-domain filter never
    runs — so their titles were counted at the 1.6x typed-intent weight.
    """
    dom = registered_domain(url)
    if dom not in SEARCH_HOSTS:
        return None
    try:
        q = parse_qs(urlparse(url).query).get("q", [""])[0].strip()
    except ValueError:
        q = ""
    return q or (title.strip() or None)


def domain_quality(dom):
    for known, mult in config.READING_DOMAINS.items():
        if dom == known or dom.endswith("." + known):
            return mult
    return 0.0


def looks_like_an_article(url, title):
    """Heuristic for 'a page worth reading' on a domain we don't recognise.

    Requires a real title and a path with some depth — a slug, not a homepage
    or an app route. Crude, but it only has to beat 'keep everything'.
    """
    if len(title) < 25 or len(title.split()) < 4:
        return False
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    if path.count("/") < 2 and not re.search(r"[a-z]-[a-z]", path):
        return False
    return not re.search(r"/(login|signin|cart|checkout|account|settings|admin)\b", path, re.I)


# ---------------------------------------------------------------------------
# Term counting
# ---------------------------------------------------------------------------

def terms_from(text):
    """Unigrams + bigrams, lowercased, stopwords dropped.

    Bigrams matter more than unigrams here: 'language model' and 'last mile'
    carry topic signal that 'language' and 'mile' do not.
    """
    words = [w for w in WORD_RE.findall(text.lower()) if w not in config.STOPWORDS and len(w) > 2]
    out = list(words)
    out += [f"{a} {b}" for a, b in zip(words, words[1:])]
    return out


def build_profile(rows):
    """rows -> (stage counts, weighted term scores, domain counts, bucket scores)."""
    stats = Counter()
    stats["loaded"] = len(rows)

    # 1. drop rows with no URL at all
    rows = [r for r in rows if r["url"].startswith("http")]
    stats["after_url_check"] = len(rows)

    # 2. dedupe on canonical URL, summing visit counts. Do this BEFORE any
    #    aggregation — exports repeat the same page many times over.
    merged = {}
    for r in rows:
        key = canonical(r["url"])
        if key in merged:
            merged[key]["visits"] += r["visits"]
            stats["duplicates_merged"] += 1
            if r["title"] and len(r["title"]) > len(merged[key]["title"]):
                merged[key]["title"] = r["title"]
        else:
            merged[key] = dict(r)
    rows = list(merged.values())
    stats["after_dedupe"] = len(rows)

    # 3. split searches out — they're scored differently from page visits
    searches, visits = [], []
    for r in rows:
        q = search_query(r["url"], r["title"])
        (searches if q else visits).append((q, r) if q else (None, r))
    searches = [(q, r) for q, r in searches]
    visits = [r for _, r in visits]
    stats["searches"] = len(searches)
    stats["page_visits"] = len(visits)

    # 4. personal searches out
    kept_searches = []
    for q, r in searches:
        if any(rx.search(q) for rx in PERSONAL_RES):
            stats["searches_dropped_personal"] += 1
            continue
        if len(q.split()) < 2:          # single-word lookups are usually navigation
            stats["searches_dropped_trivial"] += 1
            continue
        kept_searches.append((q, r))
    stats["searches_kept"] = len(kept_searches)

    # 5. noise domains out, then keep only recognised reading domains or
    #    pages that look like articles
    kept_visits, domains = [], Counter()
    for r in visits:
        dom = registered_domain(r["url"])
        if not dom or is_noise_domain(dom):
            stats["visits_dropped_noise_domain"] += 1
            continue
        if any(rx.search(r["title"]) for rx in PERSONAL_RES):
            stats["visits_dropped_personal"] += 1
            continue
        quality = domain_quality(dom)
        if quality == 0.0:
            if not looks_like_an_article(r["url"], r["title"]):
                stats["visits_dropped_not_article"] += 1
                continue
            quality = 0.7               # unknown domain, but a real-looking article
        r["quality"] = quality
        domains[dom] += 1
        kept_visits.append(r)
    stats["visits_kept"] = len(kept_visits)

    # 6. weighted term counting. Each term counts ONCE per page, so a long page
    #    that repeats a word doesn't outvote five different pages about it.
    scores, evidence = Counter(), defaultdict(set)
    for q, r in kept_searches:
        # A typed search is the strongest signal of intent in the whole export.
        for t in set(terms_from(q)):
            scores[t] += 1.6
            evidence[t].add("search: " + q[:70])
    for r in kept_visits:
        bump = r["quality"] * (1.0 + min(0.5, 0.1 * (r["visits"] - 1)))
        for t in set(terms_from(r["title"])):
            scores[t] += bump
            evidence[t].add(f"{registered_domain(r['url'])}: {r['title'][:70]}")

    # 7. sort surviving terms into the taxonomy
    buckets, unplaced = defaultdict(lambda: Counter()), Counter()
    for term, sc in scores.items():
        if sc < 1.5:                    # one weak sighting isn't an interest
            continue
        placed = False
        for topic, keywords in config.TOPIC_TAXONOMY.items():
            for kw in keywords:
                # Same word-boundary rule the scorer uses. Substring matching
                # here filed 'khasra' under conversational_ai (via 'asr') and
                # 'reddit' under ecommerce_logistics (via 'edd').
                if config.kw_pattern(kw).search(term):
                    buckets[topic][term] += sc
                    placed = True
                    break
        if not placed:
            unplaced[term] += sc

    return stats, scores, domains, buckets, unplaced, evidence


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_derived(buckets, unplaced, out_path):
    """Emit a proposal shaped like interests.json, for merging by hand."""
    totals = {t: sum(c.values()) for t, c in buckets.items()}
    top = max(totals.values()) if totals else 1.0
    topics = []
    for topic, total in sorted(totals.items(), key=lambda kv: -kv[1]):
        evidenced = [t for t, _ in buckets[topic].most_common(18)]
        topics.append({
            "name": topic,
            "label": topic.replace("_", " "),
            # Scale to the same 0-3 range interests.json uses, relative to the
            # strongest bucket, rounded to something a human would type.
            "weight": round(max(0.5, 3.0 * (total / top)) * 2) / 2,
            "enabled": total >= 0.08 * top,
            "evidence_score": round(total, 1),
            "include": evidenced,
            "boost": [],
            "mute": [],
            "queries": evidenced[:4],
        })
    payload = {
        "_provenance": f"DERIVED from browsing history by history_profile.py on {NOW:%Y-%m-%d}. "
                       "A proposal, not a profile — review it, then merge what you agree with "
                       "into interests.json. Weights are relative to the strongest bucket.",
        "topics": topics,
        "unplaced_terms": [
            {"term": t, "score": round(s, 1)} for t, s in unplaced.most_common(60)
        ],
        "_unplaced_note": "Frequent terms the taxonomy in config.py could not place. "
                          "Real interests hide here — add the good ones to TOPIC_TAXONOMY "
                          "or straight into an interests.json topic.",
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_report(stats, domains, buckets, unplaced, evidence, out_path, sources):
    L = []
    L.append("# Browsing-history interest profile\n")
    L.append(f"Generated {NOW:%Y-%m-%d %H:%M UTC} from: {', '.join(sources)}\n")

    L.append("\n## Row counts at every stage\n")
    L.append("| stage | rows |")
    L.append("| --- | ---: |")
    order = ["loaded", "after_url_check", "duplicates_merged", "after_dedupe",
             "page_visits", "searches", "searches_dropped_personal",
             "searches_dropped_trivial", "searches_kept",
             "visits_dropped_noise_domain", "visits_dropped_personal",
             "visits_dropped_not_article", "visits_kept"]
    for k in order:
        L.append(f"| {k.replace('_', ' ')} | {stats.get(k, 0):,} |")
    kept = stats.get("searches_kept", 0) + stats.get("visits_kept", 0)
    loaded = stats.get("loaded", 0) or 1
    L.append(f"\n**{kept:,} of {loaded:,} rows survived** "
             f"({100.0 * kept / loaded:.1f}%). Everything dropped is counted above — "
             "no rows were discarded silently.\n")

    L.append("\n## Topics found, strongest first\n")
    totals = sorted(((t, sum(c.values())) for t, c in buckets.items()), key=lambda kv: -kv[1])
    if not totals:
        L.append("_Nothing matched the taxonomy. Either the export is small, or "
                 "TOPIC_TAXONOMY in config.py doesn't cover what you actually read — "
                 "check the unplaced terms below._\n")
    for topic, total in totals:
        L.append(f"\n### {topic.replace('_', ' ')} — score {total:.0f}\n")
        for term, sc in buckets[topic].most_common(12):
            ev = sorted(evidence.get(term, []))[:1]
            L.append(f"- **{term}** ({sc:.0f})" + (f" — e.g. {ev[0]}" if ev else ""))

    L.append("\n## Terms the taxonomy couldn't place\n")
    L.append("_These are the gaps. Scan for real interests and add them to "
             "`TOPIC_TAXONOMY` in config.py or to a topic in interests.json._\n")
    for term, sc in unplaced.most_common(40):
        L.append(f"- {term} ({sc:.0f})")

    L.append("\n## Top reading domains\n")
    for dom, n in domains.most_common(30):
        L.append(f"- {dom} — {n} pages")

    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chrome-history", help="path to Chrome's History SQLite file")
    ap.add_argument("--takeout", help="Takeout MyActivity.json/.html, or the Takeout folder")
    ap.add_argument("--days", type=int, default=365, help="only consider the last N days (default 365)")
    args = ap.parse_args()

    if not args.chrome_history and not args.takeout:
        ap.error("give --chrome-history and/or --takeout")

    cutoff = NOW - timedelta(days=args.days) if args.days else None
    rows, sources, raw_total = [], [], 0

    if args.chrome_history:
        got, raw = load_chrome(args.chrome_history, cutoff)
        print(f"chrome:  {raw:,} rows in file, {len(got):,} within last {args.days} days")
        rows += got
        raw_total += raw
        sources.append(f"Chrome History ({raw:,} rows)")
    if args.takeout:
        got, raw = load_takeout(args.takeout, cutoff)
        print(f"takeout: {raw:,} activities in export, {len(got):,} within last {args.days} days")
        rows += got
        raw_total += raw
        sources.append(f"Google Takeout ({raw:,} activities)")

    if not rows:
        raise SystemExit("no usable rows — check the path and the --days window")

    stats, scores, domains, buckets, unplaced, evidence = build_profile(rows)

    derived = HERE / "interests.derived.json"
    report = HERE / "history_report.md"
    write_derived(buckets, unplaced, derived)
    write_report(stats, domains, buckets, unplaced, evidence, report, sources)

    kept = stats["searches_kept"] + stats["visits_kept"]
    print(f"\nkept {kept:,} of {stats['loaded']:,} rows "
          f"({stats['duplicates_merged']:,} duplicate URLs merged)")
    print(f"topics ranked: {', '.join(t for t, _ in sorted(((t, sum(c.values())) for t, c in buckets.items()), key=lambda kv: -kv[1]))}")
    print(f"\nwrote {derived.name} (a proposal — merge it into interests.json yourself)")
    print(f"wrote {report.name} (read this first)")


if __name__ == "__main__":
    main()
