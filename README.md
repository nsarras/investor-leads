# investor-leads

Find investors who are actively funding your industry and stage, from what they posted on LinkedIn and X in the last few days.

You give it one or more industries and funding stages. It searches recent public posts and uses Claude to find two kinds of lead: investors who posted something that shows they are deploying capital (calls for pitches, new funds, fresh investments), and investors named in other people's posts, such as funding announcements. Each lead goes into a spreadsheet with a confidence level, the evidence, and a link to the post.

```
$ investor-leads run --industry "gaming, entertainment" --stage "pre-seed, seed"

Industries: gaming, entertainment
Stages:     pre-seed, seed
Window:     posts since 2026-11-07

Search plan (from model):
  X         ("investing in" OR "open to pitches") (gaming OR "game studio")
  ...
  LinkedIn  investing in gaming startups
  ...

Collecting from X...
Collecting from LinkedIn (this can take a few minutes)...
212 posts collected, 198 not seen before.
Classifying 198 posts with claude-haiku-5-5...

15 leads (6 high, 9 medium) written to:
  output/leads-gaming-entertainment-pre-seed-seed-2026-11-14.csv
  output/leads-gaming-entertainment-pre-seed-seed-2026-11-14.xlsx

API costs for this run:
  X          212 posts, 180 author profiles                  $2.86  (estimate at list prices)
  Apify      1 run, 290 items                                $0.46  (reported by Apify)
  Anthropic  52 calls, 310,000 in / 140,000 out tokens       $0.10  (estimate, claude-haiku-5-5)
  Total                                                      $3.42
```

## What you get

A CSV and an Excel file with one row per lead:

| Column | Example |
|---|---|
| Confidence | high, medium-high or medium (see below) |
| Lead type | "Wrote the post" or "Named in a post" |
| Name, Firm, Role | Ana Ruiz, Pixel Ventures, General Partner |
| Signal | seeking deals / new fund / announced investment / thesis or commentary; for named leads: made an investment / launched a fund / seeking deals |
| Intent score | 0-100: how strongly the author's post shows they are investing now |
| Industry match, Stage match | yes / no / unknown against your targets |
| Why | the evidence, in one sentence |
| Platform, Post date, Post link, Profile link | links are clickable in Excel |
| Mentions, Other posts | how many posts support this lead, with up to three more links |
| Posted by, Post text, Bio / headline | context before you reach out |

Each investor appears once, however many posts mention them: spelling variants of a name are merged, and partners named alongside their firm are folded into the firm's row. Leads are sorted strongest first.

**The two kinds of lead**

- **Wrote the post:** the author is an investor. Confidence is *high* for an explicit signal (asking for pitches, a new fund) from someone who fits your industry and stage, and *medium* for a softer signal such as an investment announcement or thesis. Individual and angel investors are ranked exactly like funds and firms.
- **Named in a post:** someone else's post, usually a funding announcement, shows them making an investment, launching a fund or looking for deals in one of your industries. Confidence is *medium* when the stage is not ruled out. Named leads never rate high, because a deal shows activity but not that they are asking for pitches.

**Medium-high is the stronger half of medium.** A medium lead is rated *medium-high* when the evidence is firmer:

- an author who says they invest at one of your stages, or whose intent score is 60 or more;
- a named investor whose kind is clear (a fund, an angel, an accelerator) and whose deal is at one of your stages, or who has launched a fund or is looking for deals;
- a named investor reported in three or more separate posts.

Add `--min-confidence medium-high` to list only these and the high leads. In a one-week test that kept 30 of 58 leads.

**Low-confidence leads are left out by default.** These are partial fits: generalists, investors whose focus is not stated, a stage that looks wrong, investors in your industry whose post is not an investing signal, and studios, publishers and other companies that fund projects without being investment firms. Add `--min-confidence low` to include them. The "All classified" sheet shows them either way, marked "low, not listed".

To appear at all, someone has to be an investor and show either a fit with your industries or an investing signal. Not listed: people who are not investors, investors whose stated focus excludes your industries, investors named in deals outside your industries or where the industry is not known, investors merely mentioned with no investing action in the post, and funds that only invest in other funds.

The Excel file has a second sheet, **All classified**, listing every post the tool read and how it was judged, so you can audit what was left out.

## Setup

Requires **Python 3.10 or newer** (the Anthropic SDK does not support older versions). Check yours with `python3 --version`. On macOS the built-in `python3` is often 3.9; install a newer one with `brew install python@3.12` or from [python.org](https://www.python.org/downloads/), then use `python3.12` in place of `python3` below.

```bash
git clone <this repo>
cd investor-leads
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
cp .env.example .env
```

Upgrading pip matters: versions before 21.3 fail on `pip install -e .` with `File "setup.py" not found`.

Then put your own API keys in `.env`:

| Key | Needed for | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | [console.anthropic.com](https://console.anthropic.com) |
| `X_BEARER_TOKEN` | X posts (optional) | [console.x.com](https://console.x.com): create an app, add credits, copy the bearer token |
| `APIFY_API_TOKEN` | LinkedIn posts (optional) | [console.apify.com](https://console.apify.com) under Integrations |

You need the Anthropic key and at least one of the other two. A platform without a key is skipped.

## Usage

```bash
# See the planned searches and the cost estimate without spending anything on X or Apify
investor-leads run --industry "gaming, entertainment" --stage "pre-seed, seed" --dry-run

# Run it
investor-leads run --industry "gaming, entertainment" --stage "pre-seed, seed"

# One platform only, a shorter window
investor-leads run --industry fintech --stage seed --platforms linkedin --days 3

# Rebuild the spreadsheet from posts already collected (no API calls)
investor-leads export --industry "gaming, entertainment" --stage "pre-seed, seed" --min-confidence low
```

| Flag | Meaning |
|---|---|
| `--industry` | One or more industries, comma-separated: `"gaming, entertainment"` |
| `--stage` | One or more funding stages, comma-separated: `"pre-seed, seed"` |
| `--days` | How far back to look (default 7) |
| `--platforms` | `x`, `linkedin`, or both (default) |
| `--min-confidence` | Weakest leads to include: `medium` (default), `medium-high`, `high`, or `low` to add partial fits |
| `--searches` | Searches to run on each platform (default 12) |
| `--posts-per-search` | Posts to read from each search (default 25) |
| `--only-new` | List only leads from posts found for the first time in this run |
| `--email` | Email the spreadsheet to the recipients in `config.yaml` |
| `--email-to` | Comma-separated recipients, instead of the ones in `config.yaml` |
| `--dry-run` | Show the plan and cost estimate, then stop |
| `--replan` | Ask the model for a fresh set of searches |
| `--config` | Path to a YAML config file (default `config.yaml` if present) |

An investor counts as a match if they cover any one of the industries and any one of the stages. Results are stored per combination of industries and stages, and the order you list them in does not matter.

For anything else (caps, model, your own search queries, a different LinkedIn provider), copy `config.example.yaml` to `config.yaml` and edit it.

## Run it every day and get the results by email

```bash
investor-leads run --days 1 --only-new --email
```

- `--days 1` searches the last day.
- `--only-new` lists only leads from posts the tool has not seen on an earlier run, so nothing repeats from one day to the next.
- `--email` sends the spreadsheet to the addresses under `email.to` in `config.yaml`, with a short summary and the run's costs in the body. On a day with nothing new it sends a one-line note instead.

Email goes through any SMTP server. Add these to `.env`:

| Setting | Value |
|---|---|
| `SMTP_HOST`, `SMTP_PORT` | your mail server, e.g. `smtp.gmail.com` and `587` |
| `SMTP_USERNAME` | the sending address |
| `SMTP_PASSWORD` | the mail password; for Gmail, an [app password](https://myaccount.google.com/apppasswords), not your normal one |

To try the email without a new search, send the current spreadsheet to yourself: `investor-leads export --industry ... --stage ... --email-to you@example.com`.

**To schedule it,** [`examples/daily-github-actions`](examples/daily-github-actions) has a ready-made GitHub Actions workflow that runs every morning in the cloud, with step-by-step setup. Any other scheduler (cron, launchd) works too: run the command above from the project folder. Posts are classified once, so repeat runs only pay for what is new.

## How it works

```
industries + stages
      |
      v
 1. Plan      Claude writes search queries for each platform (once, then cached)
      |
      v
 2. Collect   X recent search API  +  LinkedIn post search via Apify
      |
      v
 3. Store     SQLite in data/leads.db, de-duplicated by post URL
      |
      v
 4. Classify  Claude reads each post + author bio: is the author an investor, and
              which other investors does the post name?
      |
      v
 5. Gate      plain code decides what counts as a lead and how confident it is
      |
      v
 6. Export    CSV + Excel in output/
```

This is a scripted pipeline, not an agent. Code decides what runs and in what order. The model does two bounded jobs: writing the search queries, and reading posts. That keeps runs repeatable, cheap, and easy to debug.

The rules for what counts as a lead, and at what confidence, live in `author_confidence()` and `named_confidence()` in `src/investor_leads/classifier.py`. They are a few lines each and meant to be edited.

## Costs and limits

You pay each provider directly. Every run ends with a summary of what it used on each API. The X and Anthropic amounts are estimates at list prices (edit them in `src/investor_leads/config.py` and `costs.py` if prices change); the Apify amount is what Apify reports for the run. Each provider's billing page has the final figure.

| Provider | What is billed | With default settings |
|---|---|---|
| X API | $0.005 per post returned, plus $0.010 per author profile | At most $4.50 per run (12 searches x 25 posts). The run prints the estimate first and skips X if it exceeds `x.max_cost_usd`. |
| Apify (LinkedIn) | Set by the Actor; the default lists $1.60 per 1,000 posts | At most 300 posts per run (12 searches x 25 posts), with a $3 spending limit passed to Apify for each Actor run. Set `linkedin.max_cost_usd` for a budget across the whole run |
| Anthropic | Tokens; the default model is Claude Haiku 5.5 | Typically cents per run |

Things to know:

- **X only searches the last 7 days.** `--days` above 7 applies to LinkedIn only. To cover longer periods on X, run the tool at least weekly; results accumulate in the local database.
- **Searches are capped, not exhaustive.** Each run reads a bounded number of posts per search. Use `--searches` and `--posts-per-search` (or `config.yaml`) for more coverage. Many narrow searches find more investors than a few broad ones.
- **Set a LinkedIn budget for big runs.** `linkedin.max_cost_usd` in `config.yaml` is the most the tool will spend on Apify in one run. Keywords are searched in order of how often their posts turned into leads over the last 30 days, so when the budget runs out it is the weakest keywords that go unsearched. The cost summary then shows how many keywords were reached. In a one-week test, a third of the posts held every lead, so a budget well below the worst case loses little.
- **LinkedIn has been the better source in testing.** Investors and funding news are concentrated there; X returns more off-topic posts for the same spend.
- **X replies are skipped** (`x.exclude_replies`), since they are mostly conversation.
- **Setting `x.include_author_profile: false`** cuts X cost by two thirds, but the model then cannot see the author's bio and will miss investors who do not say so in the post.
- **The model can be wrong.** Check the "Why" column and the post itself before reaching out.
- **X searches exclude a few spam words** (`airdrop`, `giveaway`, `presale`, `memecoin`). Change the list with `x.exclude_terms` in `config.yaml`.

## Using a different LinkedIn provider

LinkedIn offers no public API for searching posts, so the tool calls a third-party Apify Actor. The default is [`atomus/linkedin-post-search`](https://apify.com/atomus/linkedin-post-search), which needs no LinkedIn login or cookie.

These Actors change and sometimes break. To switch, set `linkedin.actor_id` in `config.yaml` and describe the new Actor's input and output under `linkedin.input` and `linkedin.field_map`. No code changes are needed. Prefer Actors that do not ask for your LinkedIn session cookie.

## Responsible use

- The tool reads public posts only. It does not log in to LinkedIn or X as you.
- You are responsible for complying with the terms of each service you use, including LinkedIn's User Agreement, X's Developer Agreement, and Apify's terms.
- `data/` and `output/` contain personal data about post authors. Both are git-ignored. Keep them local, keep only what you need, and follow the privacy law that applies to you (for example GDPR or CCPA).
- Use the results for considered, individual outreach. Do not use them for bulk messaging.

Nothing here is legal advice.

## Troubleshooting

| Message | Fix |
|---|---|
| `File "setup.py" not found. Directory cannot be installed in editable mode` | pip is too old. Run `python -m pip install --upgrade pip`, then install again. |
| `requires a different Python: 3.9.x not in '>=3.10'` | The virtual environment was created with an old Python. Delete `.venv` and recreate it with Python 3.10 or newer. |
| `investor-leads: command not found` | Activate the virtual environment: `source .venv/bin/activate`. |

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tested on Python 3.10, 3.12 and 3.13.

The tests use stubbed HTTP transports and a fake model, so they need no API keys and make no network calls.

```
src/investor_leads/
  cli.py            command-line entry point
  config.py         settings and API keys
  pipeline.py       the steps, in order
  planner.py        industries + stages -> search queries (model)
  collectors/x.py   X recent search
  collectors/linkedin.py   LinkedIn via an Apify Actor
  classifier.py     post -> structured judgment (model), and the lead rules (code)
  llm.py            Anthropic structured-output wrapper
  store.py          SQLite storage
  export.py         merge leads per investor, CSV and Excel output
  costs.py          end-of-run API cost summary
  emailer.py        email the results over SMTP
examples/daily-github-actions/   scheduled daily run on GitHub Actions
```

## Roadmap

- Track a seed list of known investors and pull their recent posts directly
- Optional enrichment step for top leads (fund thesis, recent deals)
- Google Sheets export

## License

MIT. See [LICENSE](LICENSE).
