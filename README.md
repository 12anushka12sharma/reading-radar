# Reading Radar

A daily digest of science / research / tech reading, scored against an explicit
interest profile. Papers, blogs, news and discussion in one page, ranked, with
everything off-topic **dropped** rather than politely ranked last.

Stdlib Python only — no `pip install`, no venv, no API keys, no accounts. A
scheduled job that needs dependencies is a scheduled job that silently stops
running after the next OS upgrade.

```
interests.json       the profile that drives everything
radar.py             fetch all sources -> score -> dedupe -> items.json
digest.py            items.json -> digest.html + library.html + digest.md
history_profile.py   your browsing history -> a proposed profile
archive.jsonl        append-only record of everything ever surfaced
config.py            feeds, thresholds, and the profiler's vocabulary
```

## Install

**Requirements: Python 3.8+. That is the whole list.**

```bash
git clone <your-repo-url> reading-radar   # or click "Use this template"
cd reading-radar
python3 test_radar.py                     # expect "all checks passed"
```

macOS already ships a usable `python3`; check with `python3 -V`. You may be
prompted once to install Command Line Tools — accept it.

## First run

```bash
python3 radar.py --check-feeds   # 1. are the sources alive?
python3 radar.py --dry-run       # 2. what would today surface? writes nothing
python3 radar.py                 # 3. fetch + score for real
python3 digest.py --open         # 4. render and open digest.html
```

Steps 1 and 2 write nothing, so run them freely. Step 1 should end in
`all feeds alive`; if a feed has rotted it names the line to fix in
`config.py`. Step 2 prints the ranked list so you can sanity-check the profile
before committing to it.

## Daily use

```bash
./run_daily.sh        # both stages, logs to run.log
open digest.html      # today's issue
open library.html     # everything ever surfaced, searchable
```

## Make it yours

`interests.json` is the only file that really matters. A topic's `queries`
decide what gets **fetched**; its `include` / `boost` / `mute` keywords decide
what **scores**.

| field | effect |
| --- | --- |
| `weight` | 3.0 = core, 2.0 = strong, 1.0 = only if excellent, `enabled: false` = off |
| `include` | any match puts the item in this topic |
| `boost` | +40% each (max 2) — markers of a *good* piece, not a relevant one |
| `mute` | any match drops the item from this topic |
| `require_any` | optional gate — the topic fires only if one of these also matches |
| `queries` | fed to arXiv, Hacker News and Google News |
| top-level `mute` | any match drops the item entirely |

Scoring is `sum over matching topics(weight × density) × source trust × recency`.
An item matching **no** topic scores zero and is dropped — that, not ranking,
is what keeps noise out.

Keywords match on word boundaries with plural/`-ed`/`-ing` tolerance only. So
`agent` matches `agents` but not `management`, and `eval` matches `evals` but
**not** `evaluated`. That strictness is deliberate: looser matching had `rag`
hitting `storage`. Add word variants explicitly rather than loosening it.

`require_any` exists because some words are only domain-specific in context.
`agent`, `benchmark` and `evaluation` mean one thing in an LLM paper and
something else in a biology paper — a bioRxiv item about "benchmarking
fluorescent imaging **agents**" otherwise matches an LLM topic on four
keywords. Reach for it instead of an ever-growing list of mute phrases.

Knobs in `config.py`: `DIGEST_MIN_SCORE` (raise if noisy), `DIGEST_MAX_ITEMS`,
`LOOKBACK_HOURS`, `MAX_PER_SOURCE`, `HN_MIN_POINTS`.

## Build a profile from your own reading

Hand-writing a profile is fine. Deriving one from what you actually read is
much better.

```bash
# Chrome, macOS. Quit Chrome or let the script work on its copy.
python3 history_profile.py \
  --chrome-history ~/Library/Application\ Support/Google/Chrome/Default/History \
  --days 365
```

**Check which profile folder is yours first** — `Default` is not always the one
you use, and profiling the wrong one produces a profile built from whatever
admin tooling you happen to use at work:

```bash
for p in Default "Profile 1" "Profile 2"; do python3 -c "
import json;d=json.load(open('$HOME/Library/Application Support/Google/Chrome/$p/Preferences'))
print('$p', (d.get('account_info') or [{}])[0].get('email','?'))" 2>/dev/null; done
```

Google Takeout works too and covers every device plus typed searches:
`python3 history_profile.py --takeout ~/Downloads/Takeout`.

You get two files:

- **`history_report.md`** — read this first. Row counts at every filter stage,
  topics ranked by evidence, terms the taxonomy couldn't place, top domains.
- **`interests.derived.json`** — a *proposal*. Nothing is applied automatically.

Merge what you agree with into `interests.json` by hand. The profiler
deliberately never writes that file: a profile that silently rewrites itself
from noisy input is worse than a slightly stale one, because you can't tell
when it drifted.

**Nothing leaves your machine.** The profiler reads a local copy of the browser
database. Both outputs are gitignored, and so is `archive.jsonl` — all three
describe what you read.

### What the profiler throws away

Rows with no URL → duplicate URLs (merged, counted) → searches split from page
visits → personal searches (health, money, shopping, travel, logins) → noise
domains (mail, social, shopping, banking, work tooling, job boards) → pages
that don't look like articles. Every drop is counted in the report; nothing
disappears silently. Term counting happens *after* URL dedupe, so one page
reloaded forty times doesn't outvote forty different pages.

## Running it automatically

**GitHub Actions (easiest, free).** `.github/workflows/daily.yml` runs the
pipeline daily and publishes to GitHub Pages. Enable it with
**Settings → Pages → Source: GitHub Actions**. Actions minutes are unlimited on
public repos and a run takes about three minutes. Edit the `cron:` line to
change the time — it is always UTC.

The workflow commits `seen.json` and `archive.jsonl` back to the repo. That is
the pipeline's memory; without it every run starts blank and re-surfaces the
same items.

**Locally on macOS**, prefer `launchd` over `cron`: launchd runs a missed job
when the Mac wakes, whereas cron just skips it, which on a laptop is most
mornings. Note that macOS blocks scheduled jobs from reading `~/Documents`,
`~/Desktop` and `~/Downloads` unless you grant Full Disk Access to `/bin/bash` —
a job there fails with `Operation not permitted` and no prompt. Keeping the
checkout elsewhere avoids the problem entirely.

## Sources

27 curated RSS feeds (Quanta, Nature, Science, bioRxiv, MIT Tech Review,
DeepMind, Google Research, Simon Willison, Lobsters, …), arXiv
`cs.CL`/`cs.AI`/`cs.HC`/`cs.IR`, Hacker News by keyword above a points floor,
and Google News RSS by keyword. No API keys for any of it.

Each feed carries a trust weight (0.7–1.4) multiplying the score — editorial
trust, not topic fit: a Quanta piece and a random blog post on the same topic
are not equally worth ten minutes.

`parse_feed` handles RSS 2.0, **RSS 1.0/RDF** and Atom. The RDF case matters:
Nature, Science, bioRxiv and Nature Biotechnology all use it, and a parser that
only knows RSS 2.0 returns zero items for them while they look perfectly
reachable. `fetch` also transparently inflates gzip (some CDNs send it
unasked) and follows 308 redirects (urllib only learned those in Python 3.11).

Dedupe is exact-match on canonical URL plus a 0.75 Jaccard overlap on title
words, catching the same paper arriving via arXiv and HN. `seen.json` remembers
45 days so nothing is sent twice.

## The page

`digest.html` is a pixel broadsheet: newspaper structure for readability, retro
styling confined to the furniture. A theme toggle cycles auto/light/dark, topic
chips filter the view, and each item gets a cover generated from a hash of its
URL with its topic glyph on top — generated rather than fetched, because arXiv
carries no images, hotlinking would make the page call out to publishers every
time it opens, and inlining real images costs megabytes an issue.

`library.html` is everything ever surfaced, with full-text search, a date
window and a topic filter. It embeds its rows and filters in the browser,
because a `file://` page cannot fetch a sibling file.

## When it goes wrong

| symptom | cause |
| --- | --- |
| digest empty | run `--check-feeds`; else `DIGEST_MIN_SCORE` too high, or `seen.json` has it all |
| same story repeatedly | near-dup threshold too strict — lower `0.75` in `dedupe()` |
| one source dominating | lower `MAX_PER_SOURCE` or its weight in `RSS_FEEDS` |
| off-topic items | add to top-level `mute`, tighten an `include`, or add `require_any` |
| good stuff missing | check `history_report.md` unplaced terms; widen `include`/`queries` |
| `database is locked` | quit Chrome, or point at a copy of the History file |
| scheduled run does nothing | on macOS, Full Disk Access (see above) |

## Files, and which are gitignored

Tracked: `config.py`, `interests.json`, `interests.starter.json`, `radar.py`,
`digest.py`, `history_profile.py`, `run_daily.sh`, `test_radar.py`, this README.

Ignored: `items.json`, `digest.html`, `digest.md`, `library.html`,
`interests.derived.json`, `history_report.md`, `run.log`, `.env`. On GitHub
Actions, `seen.json` and `archive.jsonl` are force-added so the memory persists;
locally they stay out of your way.

## Licence

MIT. Do what you like with it.
