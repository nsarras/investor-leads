# Run it every day and get the results by email

This sets up a scheduled job on GitHub Actions that, every morning:

1. searches posts from the last day,
2. keeps only leads from posts it has not seen before,
3. emails the spreadsheet to the people you list (or a one-line note when there is nothing new).

It runs in GitHub's cloud, so your computer does not need to be on.

## Use a private repository

Keep the schedule in a **private** repository of its own. The job stores a small database of
posts between runs, its logs show what you are searching for, and `config.yaml` holds your
recipients' addresses. None of that belongs in a public repository. The private repository
contains only two files; the tool itself is installed from its public repository on each run.

## Setup

1. **Create a private repository** on GitHub, for example `investor-leads-daily`.

2. **Add two files** from this folder:

   | File here | Where it goes in your repository |
   |---|---|
   | `daily.yml` | `.github/workflows/daily.yml` |
   | `config.yaml` | `config.yaml` |

3. **Edit `config.yaml`:** your industries, stages and the email recipients.

4. **Edit the top of `daily.yml`** if needed: `TOOL_REPO` (where to install the tool from),
   the time and time zone, and the mail server if you do not use Gmail.

5. **Add your keys as secrets.** In the repository: Settings, then Secrets and variables, then
   Actions, then "New repository secret".

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your Anthropic API key (required) |
   | `X_BEARER_TOKEN` | your X API bearer token (leave out to skip X) |
   | `APIFY_API_TOKEN` | your Apify API token (leave out to skip LinkedIn) |
   | `SMTP_USERNAME` | the address the email is sent from, e.g. you@gmail.com |
   | `SMTP_PASSWORD` | the mail password; for Gmail, an app password (see below) |

6. **Test it.** Open the Actions tab, choose "Daily investor leads", and click "Run workflow".
   The email should arrive when the run finishes.

### Gmail app password

Gmail does not accept your normal password from scripts. Create an app password instead:

1. Turn on 2-Step Verification for the Google account, if it is not already on.
2. Go to https://myaccount.google.com/apppasswords, create one (any name), and copy the
   16 characters shown.
3. Use that as `SMTP_PASSWORD`. You can revoke it on the same page at any time.

For another provider, set `SMTP_HOST` and `SMTP_PORT` in `daily.yml` to its SMTP server
(port 587 or 465) and use that account's SMTP username and password.

## Good to know

- **What "new" means.** A lead is listed when it comes from a post the job has not seen on an
  earlier day. The very first run has nothing to compare against, so it reports everything
  from the last day or two.
- **If a run fails**, GitHub emails the repository owner, nothing is saved, and the next run
  picks those posts up again. The Actions tab has the log.
- **Costs.** Each email ends with what the run used on each API. LinkedIn at this volume needs
  a paid Apify plan. A private repository also uses GitHub Actions minutes; a run typically
  takes 10 to 30 minutes, most of it waiting for the LinkedIn search.
- **Timing is not exact.** GitHub can start scheduled runs late and, when it is busy, can skip
  one altogether. The start of every hour is its busiest time, so the workflow starts at
  6:37 AM, not on the hour, and has two backup times later in the morning. A backup does
  nothing and sends nothing if a run has already succeeded that day.
- **Change the time** by editing the `cron` and `timezone` lines in `daily.yml`. Keep the
  minutes away from `0`.
- **Run it by hand** any time with the "Run workflow" button. A manual run always goes ahead;
  because only new posts are reported, a second run on the same day usually sends the "no new
  leads" note.
- **Check what happened** on the Actions tab. Each run shows whether it was started by the
  schedule or by hand, and the "Has today's run already happened?" step says why a backup
  stopped early.
