"""Shared configuration for the Reading Radar pipeline.

Two jobs in this file:
  1. Mechanics for the daily run  — thresholds, feeds, source weights.
  2. Vocabulary for history_profile.py — what counts as noise, what counts as
     reading, and the topic taxonomy it sorts terms into.

The *interest profile itself* lives in interests.json, not here. This file is
how the machine works; that file is what you are actually interested in.
"""
import re

# ---------------------------------------------------------------------------
# Keyword matching — shared by both stages
# ---------------------------------------------------------------------------

_KW_CACHE = {}


def kw_pattern(kw):
    """Word-boundary match with light plural/tense tolerance.

    Substring matching would score 'storage' as a hit for 'rag' and 'management'
    as a hit for 'agent'; this avoids both while still matching 'agents'.

    Lives here, not in radar.py, because history_profile.py needs the identical
    rule when it sorts terms into TOPIC_TAXONOMY. Two copies drifted once
    already: scoring was strict while taxonomy placement was still substring,
    which filed 'khasra' under conversational_ai via 'asr'.
    """
    if kw not in _KW_CACHE:
        _KW_CACHE[kw] = re.compile(
            r"(?<!\w)" + re.escape(kw) + r"(?:s|es|ed|ing)?(?!\w)", re.I)
    return _KW_CACHE[kw]


# ---------------------------------------------------------------------------
# Daily run knobs
# ---------------------------------------------------------------------------

LOOKBACK_HOURS = 36          # how far back a feed item may be published and still count
DIGEST_MAX_ITEMS = 24        # hard cap, so the digest stays readable in one sitting
DIGEST_MIN_SCORE = 1.6       # below this an item is noise, however fresh
SEEN_RETENTION_DAYS = 45     # how long seen.json remembers a URL before forgetting it
MAX_PER_SOURCE = 6           # stops one chatty feed from eating the whole digest
HTTP_TIMEOUT = 25
HTTP_WORKERS = 8             # feeds are fetched in parallel; keep modest to stay polite

# Some sites 403 a bare urllib User-Agent (Reddit especially).
USER_AGENT = "ReadingRadar/1.0 (personal daily digest; +https://github.com/topics/rss)"

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
# weight multiplies an item's topic score. It encodes *editorial trust*, not
# topic fit: a Quanta piece and a random blog post on the same topic are not
# equally worth her 10 minutes. Keep it in roughly 0.7 - 1.4.

RSS_FEEDS = [
    # --- general science / research journalism -----------------------------
    ("Quanta Magazine",        "https://api.quantamagazine.org/feed/",                              1.35),
    ("Nature — news",          "https://www.nature.com/nature.rss",                                 1.30),
    ("Science — news",         "https://www.science.org/rss/news_current.xml",                      1.30),
    ("Phys.org",               "https://phys.org/rss-feed/",                                        0.85),
    ("ScienceDaily",           "https://www.sciencedaily.com/rss/top/science.xml",                  0.80),
    ("MIT Technology Review",  "https://www.technologyreview.com/feed/",                            1.15),
    ("Ars Technica",           "https://feeds.arstechnica.com/arstechnica/index",                   1.00),

    # --- life science / biotech (BS Biotechnology, Velbiom probiotics) -----
    ("bioRxiv — new results",  "https://connect.biorxiv.org/biorxiv_xml.php?subject=all",           1.05),
    ("Nature Biotechnology",   "https://www.nature.com/nbt.rss",                                    1.20),
    ("STAT News",              "https://www.statnews.com/feed/",                                    0.95),

    # --- AI / ML research + engineering -----------------------------------
    ("Google Research",        "https://research.google/blog/rss/",                                 1.20),
    ("DeepMind",               "https://deepmind.google/blog/rss.xml",                              1.25),
    ("OpenAI",                 "https://openai.com/news/rss.xml",                                   1.10),
    ("Hugging Face",           "https://huggingface.co/blog/feed.xml",                              1.00),
    ("Simon Willison",         "https://simonwillison.net/atom/everything/",                        1.20),
    ("Sebastian Raschka",      "https://magazine.sebastianraschka.com/feed",                        1.20),
    ("BAIR Berkeley",          "https://bair.berkeley.edu/blog/feed.xml",                           1.15),
    ("Machine Learning Mastery","https://machinelearningmastery.com/feed/",                          0.75),

    # --- product / engineering craft (lower weight: adjacent, not core) ---
    ("Stratechery (free)",     "https://stratechery.com/feed/",                                     1.00),
    ("Pragmatic Engineer",     "https://blog.pragmaticengineer.com/rss/",                           0.95),
    ("Lenny's Newsletter",     "https://www.lennysnewsletter.com/feed",                             0.85),
    # a16z dropped 2026-10-23: every feed path 404s and the homepage advertises
    # no RSS/Atom link at all. It was the lowest-weight feed here and showed up
    # nowhere in her reading history, so not worth replacing.

    # --- AEO / GEO / search (added 2026-10-23) ----------------------------
    # aeo_geo is a weight-3.0 topic but NO feed here could carry it — it would
    # have arrived only via the arXiv/HN/Google News queries. These are the
    # trade press for the field. Verified alive; Roundtable is RSS 1.0.
    ("Google Search Central",  "https://developers.google.com/search/blog/feed.xml",                1.15),
    ("Search Engine Roundtable", "https://www.seroundtable.com/index.rdf",                          0.90),
    ("Search Engine Journal",  "https://www.searchenginejournal.com/feed/",                         0.85),

    # --- community aggregators (high volume, so scored strictly) ----------
    ("Lobsters",               "https://lobste.rs/rss",                                             0.90),
    ("r/MachineLearning",      "https://www.reddit.com/r/MachineLearning/top/.rss?t=day",            0.85),
    # She actually reads this one — 14 pages in the history run.
    ("TLDR tech",              "https://tldr.tech/api/rss/tech",                                    0.90),
]

# arXiv categories swept every run, newest-first. cs.CL = computation and
# language, cs.AI, cs.HC = human-computer interaction, cs.IR = information
# retrieval. These four cover the LLM/agent/voice/search space she works in.
ARXIV_CATEGORIES = ["cs.CL", "cs.AI", "cs.HC", "cs.IR"]
ARXIV_MAX_PER_CATEGORY = 40   # fetched, not kept — scoring culls these hard

# Hacker News is queried by keyword rather than scraped front-page, so the
# signal is topical instead of whatever is trending.
HN_MIN_POINTS = 40            # below this it hasn't been vetted by anyone yet

# Google News RSS is the flexible catch-all: anything a topic query can express.
GOOGLE_NEWS_LOCALE = "hl=en-IN&gl=IN&ceid=IN:en"

# ---------------------------------------------------------------------------
# history_profile.py vocabulary
# ---------------------------------------------------------------------------

# Domains that are never "reading" — mail, money, shopping, streaming, work
# tools, social feeds. Matched as a suffix on the registered domain.
NOISE_DOMAINS = {
    # mail / chat / calendar / docs
    "mail.google.com", "gmail.com", "calendar.google.com", "meet.google.com",
    "docs.google.com", "drive.google.com", "sheets.google.com", "slack.com",
    "web.whatsapp.com", "teams.microsoft.com", "zoom.us", "outlook.com",
    "outlook.office.com", "outlook.office365.com",
    # social / entertainment
    "instagram.com", "facebook.com", "youtube.com", "netflix.com",
    "hotstar.com", "primevideo.com", "spotify.com", "pinterest.com",
    "snapchat.com", "tiktok.com", "x.com", "twitter.com",
    # shopping / food / travel / money
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com", "ajio.com",
    "nykaa.com", "swiggy.com", "zomato.com", "blinkit.com",
    "zepto.app", "bigbasket.com", "makemytrip.com", "goibibo.com",
    "irctc.co.in", "booking.com", "airbnb.com", "airbnb.co.in", "uber.com", "olacabs.com",
    "yatra.com", "flight.yatra.com", "cleartrip.com", "ixigo.com", "agoda.com",
    "paytm.com", "phonepe.com", "razorpay.com", "hdfcbank.com", "icicibank.com",
    "sbi.co.in", "zerodha.com", "groww.in", "kite.zerodha.com",
    # accounts / identity / admin
    "accounts.google.com", "myaccount.google.com", "login.microsoftonline.com",
    "incometax.gov.in", "uidai.gov.in", "epfindia.gov.in", "passportindia.gov.in",
    # work tooling (real work, but not reading)
    "jira.com", "atlassian.net", "figma.com", "shopify.com", "admin.shopify.com",
    "localhost", "127.0.0.1", "console.cloud.google.com", "github.com/notifications",
    # job hunt (covered by work/apply, not this pipeline)
    "naukri.com", "linkedin.com", "instahyre.com", "hirist.com", "glassdoor.co.in",
    # ...and the ATS platforms every listing actually lives on. Without these,
    # 305 rows of job descriptions counted as "reading" and made product_craft
    # the top-scoring interest on the strength of JD boilerplate.
    "greenhouse.io", "job-boards.greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co", "ashbyhq.com", "jobs.ashbyhq.com",
    "myworkdayjobs.com", "smartrecruiters.com", "workable.com", "recruitee.com",
    "bamboohr.com", "icims.com", "taleo.net", "successfactors.com",
    "remoterocketship.com", "weworkremotely.com", "4dayweek.io",
    "productmanagerjobboard.com", "jobaajlearnings.com", "hackajob.com",
    "wellfound.com", "angel.co", "amazon.jobs", "cutshort.io", "foundit.in",
    "ripplehire.com", "darwinbox.com", "keka.com", "zohorecruit.com",
}

# Any host whose name starts like a careers page is a listing, whatever the
# company. Checked as a prefix on the leftmost label so `careers.adobe.com` and
# `jobs.ebayinc.com` are caught without enumerating every employer.
NOISE_SUBDOMAIN_PREFIXES = ("careers", "jobs", "job-boards", "recruiting", "apply")

# Domains where a visit is strong evidence of deliberate reading. Gets a
# quality multiplier when terms are weighted.
READING_DOMAINS = {
    "arxiv.org": 1.6, "biorxiv.org": 1.5, "medrxiv.org": 1.5,
    "pubmed.ncbi.nlm.nih.gov": 1.5, "ncbi.nlm.nih.gov": 1.4,
    "nature.com": 1.5, "science.org": 1.5, "cell.com": 1.4,
    "sciencedirect.com": 1.3, "springer.com": 1.3, "wiley.com": 1.2,
    "dl.acm.org": 1.4, "ieeexplore.ieee.org": 1.4, "openreview.net": 1.4,
    "quantamagazine.org": 1.5, "technologyreview.com": 1.3,
    "news.ycombinator.com": 1.2, "lobste.rs": 1.2,
    "simonwillison.net": 1.3, "distill.pub": 1.4, "lesswrong.com": 1.1,
    "substack.com": 1.1, "medium.com": 0.9, "towardsdatascience.com": 1.0,
    "arstechnica.com": 1.1, "wired.com": 1.0, "theverge.com": 0.9,
    "stratechery.com": 1.2, "huggingface.co": 1.2, "paperswithcode.com": 1.3,
    "anthropic.com": 1.3, "openai.com": 1.2, "deepmind.google": 1.3,
    "research.google": 1.3, "ai.meta.com": 1.2, "stackoverflow.com": 0.8,
    "docs.python.org": 0.9, "scholar.google.com": 1.3,
    # Added from the first real history run — these carried real page counts
    # and were falling through to the 0.7 "unknown but article-shaped" floor.
    "sciencedaily.com": 1.2, "deeplearning.ai": 1.2, "daily.dev": 0.9,
    "tldr.tech": 1.0, "404media.co": 1.0, "techcrunch.com": 0.9,
    "mistral.ai": 1.1, "ai.google.dev": 1.1, "adk.dev": 1.0,
    "kaggle.com": 0.9, "reddit.com": 0.9,
}

# A search query matching any of these is personal, not intellectual. Dropped
# before any term counting. Deliberately blunt — a false drop costs one term,
# a false keep puts her medical history in a tracked JSON file.
PERSONAL_PATTERNS = [
    r"\b(symptom|pain|rash|fever|doctor|clinic|hospital|dose|dosage|tablet|medicine)\b",
    r"\b(salary|loan|emi|credit score|insurance|premium|tax|refund|invoice)\b",
    r"\b(near me|open now|directions|how to reach|pin ?code|address)\b",
    r"\b(price|buy|cheap|discount|coupon|sale|offer|deal|review)\b",
    r"\b(flight|hotel|train|ticket|booking|visa appointment)\b",
    r"\b(recipe|restaurant|movie|song|lyrics|episode|season \d)\b",
    r"\b(wedding|marriage|birthday|gift|dress|saree|makeup|skin ?care)\b",
    r"\b(weather|temperature|aqi|traffic)\b",
    r"\b(password|login|sign ?in|otp|reset)\b",
    # Property and land records. A "Khasra No. ... Village ... Tehsil" search
    # survived the first real run and was filed under conversational_ai.
    r"\b(khasra|khata|tehsil|patwari|village|plot no|survey no|land record|registry|mutation)\b",
    # Job seeking — work/apply owns this. Narrow on purpose: 'hiring' and
    # 'openings' are job hunting, but 'interview' and 'roadmap' are PM craft
    # and stay in.
    r"\b(hiring|vacanc(y|ies)|openings?|job board|referral|naukri)\b",
    r"\bremote\b[^.]{0,40}\b(roles?|jobs?|positions?)\b",
]

# Topic taxonomy. history_profile.py sorts the surviving terms into these
# buckets and reports what it could not place, so gaps are visible rather than
# silently dropped.
TOPIC_TAXONOMY = {
    # Answer-engine / generative-engine optimization: how brands surface
    # inside AI search results rather than classic blue links.
    "aeo_geo": [
        "aeo", "geo", "answer engine", "answer engine optimization",
        "generative engine optimization", "ai search", "ai overview",
        "ai mode", "llm visibility", "brand visibility", "search visibility",
        "llm citation", "citation", "brand mention", "share of voice",
        "zero click", "llms.txt", "schema markup", "structured data",
        "seo", "serp", "content audit", "crawlability",
        "perplexity", "chatgpt search",
    ],
    "llm_agents": [
        "llm", "language model", "agent", "agentic", "rag", "retrieval augmented",
        "prompt", "prompting", "context window", "context engineering", "tool use",
        "function calling", "mcp", "fine-tune", "fine-tuning", "lora", "eval",
        "benchmark", "hallucination", "chain of thought", "reasoning model",
        "token", "embedding", "vector database", "claude", "gpt", "gemini", "llama",
        # 'hugging face' scored 31 unplaced; huggingface.co is 27 pages. Her dev
        # reading is AI tooling, not the kubernetes/postgres in devtools_eng.
        "hugging face", "huggingface", "claude code", "mcp server", "ai sdk",
        "agent framework", "system card", "api pricing", "token cost",
    ],
    "conversational_ai": [
        "voice bot", "voicebot", "voice ai", "speech recognition", "asr",
        "text to speech", "tts", "whisper", "dialogue system", "chatbot",
        "ivr", "speech model", "diarization", "intent recognition", "nlu",
        "turn taking", "latency voice",
        # Bare 'voice' is worth having: a profiler run showed it scoring high
        # while going unplaced. Vendor names belong here too — evaluating
        # vendors is how most people actually read this space.
        "voice", "voice agent", "speech", "transcription", "barge-in",
        "vapi", "retell", "elevenlabs", "deepgram", "cartesia", "assemblyai",
    ],
    "ml_research": [
        "transformer", "attention", "neural network", "deep learning",
        "reinforcement learning", "rlhf", "interpretability", "mechanistic",
        "diffusion model", "self-supervised", "scaling law", "distillation",
        "quantization", "mixture of experts", "state space model", "neurips",
        "icml", "iclr", "acl",
    ],
    "cx_automation": [
        "customer support", "support automation", "ticketing", "deflection",
        "csat", "first response time", "contact center", "help desk",
        "zendesk", "freshdesk", "intercom", "agent assist", "customer experience",
    ],
    "ecommerce_logistics": [
        "last mile", "supply chain", "warehouse", "wms", "order management",
        "returns", "reverse logistics", "fulfilment", "fulfillment",
        "delivery estimate", "edd", "wismo", "dark store", "quick commerce",
        "d2c", "inventory",
    ],
    "biotech_life_science": [
        "biotech", "microbiome", "probiotic", "gut bacteria", "genomics",
        "crispr", "gene editing", "protein folding", "alphafold", "clinical trial",
        "immunology", "vaccine", "drug discovery", "synthetic biology",
        "molecular biology", "fermentation", "enzyme",
    ],
    "hard_science": [
        "physics", "quantum", "astronomy", "astrophysics", "cosmology",
        "black hole", "exoplanet", "neuroscience", "brain", "cognition",
        "climate", "mathematics", "number theory", "topology", "materials science",
        "superconductor", "fusion",
    ],
    "product_craft": [
        "product management", "product manager", "roadmap", "discovery",
        "user research", "a/b test", "experimentation", "pricing", "retention",
        "activation", "north star metric", "jobs to be done", "pm interview",
    ],
    "devtools_eng": [
        "python", "sql", "api design", "data pipeline", "duckdb", "pandas",
        "postgres", "observability", "system design", "latency", "caching",
        "kubernetes", "docker", "open source", "developer experience",
    ],
}

# Words that carry no topical information. Short list on purpose — the
# domain/personal filters do the heavy lifting, not stopword removal.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
    "those", "as", "at", "by", "from", "how", "what", "why", "when", "which",
    "who", "can", "do", "does", "did", "not", "no", "yes", "you", "your", "i",
    "my", "me", "we", "our", "us", "he", "she", "they", "them", "his", "her",
    "about", "into", "over", "after", "before", "more", "most", "best", "new",
    "using", "use", "used", "via", "vs", "if", "than", "then", "so", "but",
    "all", "any", "some", "get", "got", "make", "made", "will", "would",
    "should", "could", "may", "might", "google", "search", "www", "com",
    "http", "https", "html", "pdf", "amp", "one", "two", "first", "last",
}

# arXiv announces in daily batches and pauses at weekends, so papers need a
# wider window than news or the Monday digest comes up empty.
ARXIV_LOOKBACK_HOURS = 96
