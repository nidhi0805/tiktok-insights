# TikTok Insights

Drop your TikTok analytics exports into `input/`, run one command, and get a report with stats and AI-written insights from Claude.

Every run adds to a running history (`data/`), so the insights get better the longer you keep feeding it.

## Setup (one time)

You need Python 3.9+.

```bash
cd tiktok-insights
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
```

Open `.env` and paste your Anthropic API key (get one at console.anthropic.com; API usage is billed separately from a claude.ai subscription). Also fill in `NICHE` (what you post about) and `GOAL` (e.g. "reach 1,000 followers"); the insights are noticeably better with that context.

## Weekly routine

1. In TikTok Studio, open the analytics section and download your data as a CSV (the menu layout changes from time to time; look for a download/export option on the content or overview analytics pages).
2. Put the file in `input/`. **Put the export date in the file name**, like `2026-11-09_content.csv`. The script uses it to tell exports apart so it can show week-over-week growth. Without a date in the name it falls back to the file's modified date.
3. Run:

   ```bash
   python analyze.py
   ```

4. Read the result in the terminal, or open `output/report_<date>.md`.

You can leave old exports in `input/`. Re-running never double-counts, because the history is deduplicated per video per export date.

## Try it with fake data first

`sample_data/` has two made-up exports (a week apart) so you can see what a report looks like before using your own numbers. These point the history and report at throwaway folders so your real `data/` stays clean:

```bash
python analyze.py --no-ai --input-dir sample_data --data-dir /tmp/demo/data --output-dir /tmp/demo/out
```

On Windows, swap `/tmp/demo` for any scratch folder, such as `C:\temp\demo`. The sample column names are illustrative; your real export may use different ones, which the script handles (see below).

## What it understands

- **Per-video exports** (a row per video, with a title or link): top and bottom videos, engagement rate, best weekdays and times, video length, caption features, posting cadence, and growth since your previous export.
- **Daily account exports** (a row per day, no video titles): last 7 days vs the prior 7 days.

You can mix both kinds in `input/`.

## If your file isn't recognised

TikTok changes its export headers over time, so the script matches columns by name against a list of known variants and prints which ones it matched for each file. If it says a file was skipped, it prints the column names it found. Either add those names to the `ALIASES` list at the top of `analyze.py`, or send the header row to Claude and ask it to update the list.

## Options

```bash
python analyze.py --no-ai              # stats only, no API call (free)
python analyze.py --model <model-id>   # use a different Claude model
```

With no API key set, the script still writes the stats report and skips the AI step.

## A note on small accounts

With fewer than about 20 videos, most patterns are noise. The AI step is told this and will frame findings as hypotheses and suggest experiments. Treat early reports as a way to build the habit and the history.

## Privacy

`input/`, `data/`, `output/` and `.env` are in `.gitignore`, so your analytics and API key stay out of git if you push this repo. Only the summary tables and recent captions are sent to the Claude API, not the raw CSV files.

## Later: fully hands-off

Once you're posting regularly, the manual CSV step can be replaced with a pull from the TikTok Display API. The rest of the pipeline stays the same, since it only needs rows in the same shape.
