#!/usr/bin/env python3
"""Self-checks for Reading Radar.

    python3 test_radar.py

No framework, no dependencies — plain asserts, same as the rest of the project.
Every check here guards a bug that actually happened, not a hypothetical one.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config          # noqa: E402
import digest          # noqa: E402
import history_profile as hp   # noqa: E402
import radar           # noqa: E402


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------

def test_kw_pattern_rejects_substrings():
    """The whole point: 'rag' must not hit 'storage'."""
    for kw, text in [("rag", "storage"), ("asr", "khasra"), ("edd", "reddit"),
                     ("agent", "management"), ("eval", "evaluated"),
                     ("geo", "geography"), ("seo", "seoul")]:
        assert not config.kw_pattern(kw).search(text), f"{kw!r} wrongly matched {text!r}"


def test_kw_pattern_accepts_real_variants():
    for kw, text in [("agent", "agents"), ("agentic", "agentic frameworks"),
                     ("prompt", "prompt engineering"), ("eval", "evals"),
                     ("llm", "LLM visibility")]:
        assert config.kw_pattern(kw).search(text), f"{kw!r} failed to match {text!r}"


def test_variants_need_explicit_listing():
    """Tolerance is s/es/ed/ing only, by design.

    'agentic' is not a tense variant of 'agent', so the fix for a miss is to
    add the word, never to loosen the pattern.
    """
    assert not config.kw_pattern("agent").search("agentic")
    assert "agent" in config.TOPIC_TAXONOMY["llm_agents"]
    assert "agentic" in config.TOPIC_TAXONOMY["llm_agents"]


def test_taxonomy_placement_is_sane():
    """Substring placement filed 'khasra' under speech recognition via 'asr'."""
    def topics_for(term):
        return {t for t, kws in config.TOPIC_TAXONOMY.items()
                if any(config.kw_pattern(k).search(term) for k in kws)}

    assert topics_for("khasra") == set()
    assert topics_for("reddit") == set()
    assert "biotech_life_science" not in topics_for("discovery")
    assert "llm_agents" in topics_for("agentic")


# --------------------------------------------------------------------------
# History profiling
# --------------------------------------------------------------------------

def test_search_query_only_matches_real_search_hosts():
    """docs.google.com ends with google.com but is not a search.

    A suffix test routed every Docs, Drive and accounts page down the search
    path, where the noise filter never runs, and counted their titles at the
    strongest weight in the system.
    """
    assert hp.search_query("https://www.google.com/search?q=tts+quality", "") == "tts quality"
    for url in ("https://docs.google.com/spreadsheets/d/abc/edit",
                "https://accounts.google.com/signin",
                "https://drive.google.com/drive/my-drive",
                "https://mail.google.com/mail/u/0/"):
        assert hp.search_query(url, "Some Title") is None, f"{url} treated as a search"


def test_noise_domains_cover_job_listings():
    """Job boards made 'product management' the top interest off JD boilerplate."""
    for dom in ("careers.example.com", "jobs.example.com", "job-boards.greenhouse.io",
                "jobs.lever.co", "remoterocketship.com", "docs.google.com"):
        assert hp.is_noise_domain(dom), f"{dom} should be noise"
    for dom in ("arxiv.org", "sciencedaily.com", "news.ycombinator.com",
                "simonwillison.net", "reddit.com"):
        assert not hp.is_noise_domain(dom), f"{dom} should NOT be noise"


# --------------------------------------------------------------------------
# Feed parsing and transport
# --------------------------------------------------------------------------

def test_parse_feed_handles_rss1_rss2_and_atom():
    """RSS 1.0 namespaces every element; iter("item") finds only RSS 2.0.

    Nature, Science, bioRxiv and Nature Biotechnology are all RSS 1.0 and
    parsed to zero items while looking perfectly reachable.
    """
    rss1 = """<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:dc="http://purl.org/dc/elements/1.1/"
             xmlns="http://purl.org/rss/1.0/">
      <item rdf:about="https://example.com/x1">
        <title>A paper</title><link>https://example.com/x1</link>
        <description>Body.</description><dc:date>2026-10-22T00:00:00Z</dc:date>
      </item></rdf:RDF>"""
    items = radar.parse_feed(rss1)
    assert len(items) == 1, f"RSS 1.0 parsed {len(items)} items"
    assert items[0][0] == "A paper" and items[0][2] is not None

    rss2 = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Plain RSS 2.0</title><link>https://example.com/a</link>
      <description>Body.</description>
      <pubDate>Wed, 22 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    items = radar.parse_feed(rss2)
    assert len(items) == 1 and items[0][2] is not None

    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>An Atom entry</title>
      <link rel="alternate" href="https://example.com/b"/>
      <summary>Body.</summary><updated>2026-10-22T10:00:00Z</updated></entry></feed>"""
    items = radar.parse_feed(atom)
    assert len(items) == 1 and items[0][1] == "https://example.com/b"

    # an item with no usable link is dropped, not emitted half-built
    assert radar.parse_feed(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        ' xmlns="http://purl.org/rss/1.0/"><item><title>No link</title>'
        '</item></rdf:RDF>') == []


def test_fetch_decompresses_gzip():
    """Some CDNs return gzip unasked; undecompressed the feed looks dead."""
    import gzip as _gzip
    import tempfile

    feed = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
            b'<item><title>Gzipped item</title><link>https://example.com/g</link>'
            b'<description>Body.</description></item></channel></rss>')
    with tempfile.NamedTemporaryFile(suffix=".xml.gz", delete=False) as fh:
        fh.write(_gzip.compress(feed))
        path = fh.name
    body = radar.fetch("file://" + path)
    assert body is not None and body[:2] != b"\x1f\x8b", "body still compressed"
    assert len(radar.parse_feed(body)) == 1
    Path(path).unlink()


def test_fetch_follows_308():
    """urllib only learned 308 in Python 3.11; both halves of the fix are needed."""
    import urllib.request

    assert hasattr(radar._Redirect308, "http_error_308")
    assert "redirect_request" in vars(radar._Redirect308), \
        "3.9 whitelists {301,302,303,307} and re-raises 308 even once dispatched"
    assert any(isinstance(h, radar._Redirect308) for h in radar._opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler
                   for h in radar._opener.handlers)


def test_strip_boilerplate():
    """Publishing-platform furniture must not reach the headline."""
    t, s = radar.strip_boilerplate(
        "A Headline via @someblog, @anauthor",
        "A real summary. The post A Headline appeared first on Some Blog.")
    assert t == "A Headline", t
    assert s == "A real summary.", s
    _, s = radar.strip_boilerplate("Same Thing", "Same Thing")
    assert s == ""


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _interests():
    return json.loads((HERE / "interests.json").read_text())


def test_profile_scores_sensibly():
    ints = _interests()

    def score(title, summary):
        return radar.score_item(
            {"title": title, "summary": summary, "source": "x", "url": "u",
             "published": None, "source_weight": 1.0, "age_hours": 5.0}, ints)

    for t, s in [("Mechanistic interpretability of attention heads in LLMs",
                  "A study of transformer internals."),
                 ("CRISPR prime editing corrects a liver mutation in humans",
                  "A clinical trial result.")]:
        sc, topics, _ = score(t, s)
        assert topics, f"scored nothing for {t!r}"

    for t, s in [("Product Manager II - Apply now, 5 years of experience", "Job description."),
                 ("Celebrity wedding breaks box office records", "Gossip.")]:
        sc, _, _ = score(t, s)
        assert sc == 0.0, f"let through {t!r} at {sc}"


def test_require_any_gates_ambiguous_keywords():
    """'agent' and 'benchmark' are LLM terms only in context.

    A real bioRxiv paper on "benchmarking fluorescent imaging agents" matched
    an LLM topic on four keywords and landed mid-digest at weight 3.0.
    """
    ints = _interests()
    llm = next((t for t in ints["topics"] if t["name"] == "llm_agents"), None)
    assert llm and llm.get("require_any"), "llm_agents lost its require_any gate"

    sc, topics, _ = radar.score_item(
        {"title": "3D-Bioprinted Phantoms for Quantitative Nanofluorophore Benchmarking",
         "summary": "Murine models limit evaluating fluorescent imaging agents; "
                    "we report quantitative evaluation under surgical conditions.",
         "source": "x", "url": "u", "published": None,
         "source_weight": 1.0, "age_hours": 5.0}, ints)
    assert "llm_agents" not in topics, f"science paper matched llm_agents ({sc})"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_pixel_svg_and_covers():
    svg = digest.pixel_svg(["xxxx", "x..x"], px=2)
    assert svg.count("<rect") == 3 and "width='4' height='1'" in svg, "runs not merged"
    assert "shape-rendering='crispEdges'" in svg

    a1 = digest.item_cover("https://example.com/a", "llm_agents")
    assert a1 == digest.item_cover("https://example.com/a", "llm_agents"), "not deterministic"
    assert a1 != digest.item_cover("https://example.com/b", "llm_agents"), "covers collide"

    for t in _interests()["topics"]:
        assert t["name"] in digest.PIXEL_GLYPHS, f"no pixel glyph for {t['name']}"
        assert len(digest.short_label(t["name"])) <= 24, t["name"]

    assert digest.score_meter(9.5).count("class='on'") == 5
    assert digest.score_meter(0.5).count("class='on'") == 1


def test_archive_appends_once():
    """Re-running on the same day is routine and must not double-count."""
    import tempfile

    original = digest.HERE
    with tempfile.TemporaryDirectory() as tmp:
        digest.HERE = Path(tmp)
        try:
            items = [{"url": "https://example.com/a", "title": "A", "source": "s",
                      "score": 3.0, "topics": [], "published": "", "extra": {}}]
            assert digest.append_archive(items) == 1
            assert digest.append_archive(items) == 0, "re-run double-counted"
            # a torn line from an interrupted write costs one row, not the file
            with (Path(tmp) / digest.ARCHIVE).open("a") as fh:
                fh.write('{"url": "https://example.com/d", "ti')
            assert len(digest.load_archive()) == 1
        finally:
            digest.HERE = original


# --------------------------------------------------------------------------
# This repo is public
# --------------------------------------------------------------------------

def test_repo_carries_no_personal_data():
    """This repo is public and must stay generic.

    interests.json here is a starter profile. A profile derived from someone's
    real browsing history, and the archive of what they read, must never be
    committed — .gitignore covers the derived files, and this catches the rest.
    """
    # Split so the marker strings do not appear literally in this file — on the
    # first run this test flagged itself, which is funny once and useless after.
    # Split so the marker strings do not appear literally in this file — on the
    # first run this test flagged itself, which is funny once and useless after.
    # The list is the weak point: it only catches what it was told about. One
    # former-employer name was missing here and shipped in a config.py comment
    # to this public repo, where it sat for hours. Add an employer, client or
    # vendor the moment it comes up, not after.
    # Keep every entry split — writing one out in full makes this file fail
    # its own check, which already happened twice.
    markers = tuple(a + b for a, b in (
        ("aeo", "bird"), ("datafor", "seo"), ("squad", "stack"),
        ("bite", "speed"), ("lime", "chat"), ("mama", "earth"),
        ("hon", "asa"), ("anu", "shka"), ("customer", "mates"),
        ("vel", "biom"), ("unicom", "merce"), ("ozone", "tel"),
        # NB: not 'freshdesk' / 'zendesk' / 'intercom' — those are generic
        # support-tool vocabulary and belong in the cx_automation keyword list.
        # A marker has to identify a person, not name a product they use.
    ))
    for path in HERE.rglob("*"):
        if not path.is_file() or ".git/" in str(path):
            continue
        if path.suffix not in (".py", ".json", ".md", ".sh", ".yml", ".yaml", ""):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for m in markers:
            assert m not in body, f"{path.name} contains {m!r}"

    # the derived files must be ignored, not merely absent
    ignored = (HERE / ".gitignore").read_text()
    for f in ("history_report.md", "interests.derived.json", "archive.jsonl",
              "seen.json", "digest.html", "library.html"):
        assert f in ignored, f"{f} is not gitignored"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nall checks passed ({len(tests)})")
