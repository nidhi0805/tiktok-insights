#!/usr/bin/env python3
"""TikTok analytics pipeline.

Drop TikTok Studio exports (CSV/XLSX) into input/, run this script, and get:
  - a running history in data/ (so trends build up over time)
  - a stats summary
  - AI-written insights from Claude (skipped with --no-ai)
The report is written to output/report_<date>.md.

Understood export types (detected from the column headers):
  per-video rows, Overview (daily totals), Viewers, FollowerHistory,
  FollowerActivity, FollowerGender, FollowerTopTerritories.
"""
from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "claude-sonnet-5-5"

# TikTok's export headers change between versions, so columns are matched by
# normalised name (lowercase, letters/digits only, "(s)"/"(%)" ignored) against these aliases.
ALIASES = {
    "posted_at": ["postdate", "posttime", "videoposttime", "videopostdate", "createtime",
                  "createdat", "publishtime", "dateposted", "posted"],
    "date": ["date", "day"],
    "title": ["videotitle", "title", "caption", "description", "videodescription"],
    "link": ["videolink", "link", "url", "videourl"],
    "views": ["videoviews", "views", "totalviews", "playcount", "plays", "viewcount"],
    "likes": ["likes", "totallikes", "likecount"],
    "comments": ["comments", "totalcomments", "commentcount"],
    "shares": ["shares", "totalshares", "sharecount"],
    "saves": ["saves", "favorites", "bookmarks", "totalsaves", "totalfavorites", "savecount"],
    "avg_watch_s": ["averagewatchtime", "avgwatchtime", "averagewatchtimes", "avgwatchtimes"],
    "full_watch_rate": ["watchedfullvideo", "fullvideowatchedrate", "watchedfullvideorate",
                        "completionrate"],
    "new_followers": ["newfollowers", "followersgained", "totalnewfollowers"],
    "duration_s": ["videolength", "videoduration", "duration", "length"],
    "profile_views": ["profileviews"],
}
NUM_METRICS = ["views", "likes", "comments", "shares", "saves", "new_followers"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# One history file per export type. (The daily-totals file is named overview_history.csv
# so it never collides with the history from the first version of this script.)
HISTORY_FILES = {
    "video": "video_history.csv",
    "daily": "overview_history.csv",
    "viewers": "viewers_history.csv",
    "follower_history": "follower_history.csv",
    "follower_activity": "follower_activity_history.csv",
    "gender": "gender_history.csv",
    "territories": "territories_history.csv",
}
DEDUPE = {
    "video": ["video_key", "snapshot_date"],
    "daily": ["date"],
    "viewers": ["date"],
    "follower_history": ["date"],
    "follower_activity": ["date", "hour"],
    "gender": ["category", "snapshot_date"],
    "territories": ["category", "snapshot_date"],
}

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
def norm(s) -> str:
    """Lowercase letters/digits only, ignoring unit suffixes like "(s)" or "(%)"."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", str(s).lower()))


def map_columns(df: pd.DataFrame) -> dict:
    by_norm = {}
    for c in df.columns:
        by_norm.setdefault(norm(c), c)
    mapping = {}
    for canon, names in ALIASES.items():
        for n in names:
            if n in by_norm:
                mapping[canon] = by_norm[n]
                break
    return mapping


def col(df: pd.DataFrame, mapping: dict, key: str) -> pd.Series:
    if key in mapping:
        return df[mapping[key]]
    return pd.Series([pd.NA] * len(df), index=df.index)


def getn(df: pd.DataFrame, *names: str) -> pd.Series:
    """First column whose normalised name is one of `names`, else an all-empty series."""
    wanted = set(names)
    for c in df.columns:
        if norm(c) in wanted:
            return df[c]
    return pd.Series([pd.NA] * len(df), index=df.index)


def getp(df: pd.DataFrame, prefix: str) -> pd.Series:
    """First column whose normalised name starts with `prefix`, else an all-empty series."""
    for c in df.columns:
        if norm(c).startswith(prefix):
            return df[c]
    return pd.Series([pd.NA] * len(df), index=df.index)


def num(series: pd.Series) -> pd.Series:
    """Numbers from text. Things like "undefined" become empty rather than errors."""
    s = (series.astype(str).str.replace(",", "", regex=False)
         .str.replace("%", "", regex=False).str.strip())
    return pd.to_numeric(s, errors="coerce")


def seconds(series: pd.Series) -> pd.Series:
    def one(v):
        if pd.isna(v):
            return float("nan")
        t = str(v).strip().lower().rstrip("s").strip()
        if ":" in t:
            try:
                parts = [float(p) for p in t.split(":")]
            except ValueError:
                return float("nan")
            total = 0.0
            for p in parts:
                total = total * 60 + p
            return total
        try:
            return float(t.replace(",", ""))
        except ValueError:
            return float("nan")
    return series.map(one)


def _safe_date(y: int, m: int, d: int):
    try:
        return pd.Timestamp(year=y, month=m, day=d)
    except ValueError:  # e.g. Feb 29 in a non-leap year
        return pd.NaT


def _month_day(s):
    m = re.match(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2})\s*$", str(s))
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    return (mon, int(m.group(2))) if mon else None


def parse_dates(series: pd.Series, snap: str) -> pd.Series:
    """Parse dates, including TikTok's year-less ones like "November 9".

    For year-less dates the rows are assumed to be in chronological order (oldest first,
    or newest first; both are detected). The most recent row is placed on or just before
    the export date, then years are assigned by walking back and stepping down a year
    each time the month/day sequence wraps around.
    """
    md = [_month_day(s) for s in series]
    if not md or any(x is None for x in md):
        return pd.to_datetime(series, errors="coerce", format="mixed")
    snap_d = pd.Timestamp(snap)

    def latest_year(m, d):
        # Most recent year in which this month/day is not after the export date.
        y = snap_d.year
        t = _safe_date(y, m, d)
        return y - 1 if pd.notna(t) and t > snap_d + pd.Timedelta(days=2) else y

    def doy(m, d):
        t = _safe_date(2000, m, d)
        return t.dayofyear if pd.notna(t) else 0

    # Rows in date order step forward (or backward) by small amounts, even across New Year.
    fwd_bad = sum(1 for a, b in zip(md, md[1:]) if (doy(*b) - doy(*a)) % 366 > 120)
    back_bad = sum(1 for a, b in zip(md, md[1:]) if (doy(*a) - doy(*b)) % 366 > 120)
    limit = len(md) / 20
    if fwd_bad <= limit and fwd_bad <= back_bad:
        seq, descending = md, False
    elif back_bad <= limit:
        seq, descending = md[::-1], True
    else:  # not in date order (e.g. sorted by views): assume the last 12 months
        dates = [_safe_date(latest_year(m, d), m, d) for m, d in md]
        return pd.Series(pd.to_datetime(dates), index=series.index)

    year = latest_year(*seq[-1])
    years = [year]
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] > seq[i + 1]:
            year -= 1
        years.append(year)
    years.reverse()
    dates = [_safe_date(y, m, d) for y, (m, d) in zip(years, seq)]
    if descending:
        dates.reverse()
    return pd.Series(pd.to_datetime(dates), index=series.index)


def snapshot_date(path: Path) -> str:
    """Date the export was taken: a YYYY-MM-DD in the file name, else file mtime."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        return m.group(1)
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


def read_any(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, sep=None, engine="python")
        except Exception:
            continue
    raise ValueError("could not read file")


# ----------------------------------------------------------------------------
# Builders: one per export type. All take (df, mapping, snapshot_date).
# ----------------------------------------------------------------------------
def build_videos(df: pd.DataFrame, mapping: dict, snap: str) -> pd.DataFrame:
    # Some exports carry the pull date in a "Time" column (e.g. "November 9"); that is a better
    # export date than the file's modified date, so prefer it.
    t = getn(df, "time")
    if t.notna().any() and not t.astype(str).str.fullmatch(r"\s*[\d:.]+\s*").any():
        tp = parse_dates(t, snap)
        if tp.notna().all():
            snap = f"{tp.max():%Y-%m-%d}"
    v = pd.DataFrame(index=df.index)
    v["title"] = col(df, mapping, "title").fillna("").astype(str).str.strip()
    v["link"] = col(df, mapping, "link").fillna("").astype(str).str.strip()
    pcol = mapping.get("posted_at") or mapping.get("date")
    v["posted_at"] = parse_dates(df[pcol], snap) if pcol else pd.NaT
    for k in NUM_METRICS:
        v[k] = num(col(df, mapping, k))
    v["avg_watch_s"] = seconds(col(df, mapping, "avg_watch_s"))
    v["duration_s"] = seconds(col(df, mapping, "duration_s"))
    frate = num(col(df, mapping, "full_watch_rate"))
    if frate.notna().any() and frate.max() <= 1.0:
        frate = frate * 100
    v["full_watch_rate"] = frate
    v["snapshot_date"] = snap
    v["video_key"] = v["link"].where(v["link"] != "", v["title"] + "|" + v["posted_at"].astype(str))
    return v[v["views"].notna()]


def build_daily(df: pd.DataFrame, mapping: dict, snap: str) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["date"] = parse_dates(df[mapping["date"]], snap)
    for k in NUM_METRICS + ["profile_views"]:
        d[k] = num(col(df, mapping, k))
    d["snapshot_date"] = snap
    return d[d["date"].notna() & d["views"].notna()]


def build_viewers(df: pd.DataFrame, mapping: dict, snap: str) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["date"] = parse_dates(getn(df, "date", "day"), snap)
    d["total_viewers"] = num(getn(df, "totalviewers"))
    d["new_viewers"] = num(getn(df, "newviewers"))
    d["returning_viewers"] = num(getn(df, "returningviewers"))
    d["snapshot_date"] = snap
    return d[d["date"].notna()]


def build_follower_history(df: pd.DataFrame, mapping: dict, snap: str) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["date"] = parse_dates(getn(df, "date", "day"), snap)
    d["followers"] = num(getn(df, "followers"))
    d["net_change"] = num(getp(df, "difference"))
    d["snapshot_date"] = snap
    return d[d["date"].notna()]


def build_follower_activity(df: pd.DataFrame, mapping: dict, snap: str) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["date"] = parse_dates(getn(df, "date", "day"), snap)
    d["hour"] = num(getn(df, "hour"))
    d["active_followers"] = num(getn(df, "activefollowers"))
    d["snapshot_date"] = snap
    return d[d["date"].notna() & d["hour"].notna()]


def _build_distribution(df: pd.DataFrame, snap: str, label_names: tuple) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["category"] = getn(df, *label_names).astype(str).str.strip()
    share = num(getn(df, "distribution"))
    if share.notna().any() and share.sum() <= 1.5:  # fractions (0.94) -> percent
        share = share * 100
    d["share_pct"] = share
    d["snapshot_date"] = snap
    return d[d["share_pct"].notna()]


def build_gender(df, mapping, snap):
    return _build_distribution(df, snap, ("gender",))


def build_territories(df, mapping, snap):
    return _build_distribution(df, snap, ("topterritories", "territory", "country"))


BUILDERS = {
    "video": build_videos,
    "daily": build_daily,
    "viewers": build_viewers,
    "follower_history": build_follower_history,
    "follower_activity": build_follower_activity,
    "gender": build_gender,
    "territories": build_territories,
}


def detect_kind(df: pd.DataFrame, mapping: dict):
    cols = {norm(c) for c in df.columns}
    if {"hour", "activefollowers"} <= cols:
        return "follower_activity"
    if "gender" in cols and "distribution" in cols:
        return "gender"
    if "topterritories" in cols and "distribution" in cols:
        return "territories"
    if "totalviewers" in cols or {"newviewers", "returningviewers"} <= cols:
        return "viewers"
    if "followers" in cols and "date" in cols:
        return "follower_history"
    if "views" in mapping and ("link" in mapping or "title" in mapping):
        return "video"
    if "views" in mapping and "date" in mapping:
        return "daily"
    return None


def load_inputs(input_dir: Path) -> dict:
    files = sorted(p for p in input_dir.iterdir()
                   if p.suffix.lower() in (".csv", ".xlsx", ".xls") and not p.name.startswith("."))
    frames = {k: [] for k in HISTORY_FILES}
    for f in files:
        try:
            df = read_any(f)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {f.name}: skipped ({e})")
            continue
        mapping = map_columns(df)
        snap = snapshot_date(f)
        kind = detect_kind(df, mapping)
        if kind is None:
            print(f"  ! {f.name}: skipped, format not recognised. Columns found: {list(df.columns)}")
            continue
        built = BUILDERS[kind](df, mapping, snap)
        frames[kind].append(built)
        rng = ""
        if "date" in built.columns and built["date"].notna().any():
            rng = f", dates {built['date'].min():%Y-%m-%d} to {built['date'].max():%Y-%m-%d}"
        used = built["snapshot_date"].iloc[0] if len(built) else snap
        print(f"  + {f.name}: {len(built)} rows read as '{kind}' (export date {used}{rng})")
    return {k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame()) for k, v in frames.items()}


# ----------------------------------------------------------------------------
# History (appended every run, deduplicated)
# ----------------------------------------------------------------------------
def _store_dates(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("posted_at", "date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def update_history(path: Path, new: pd.DataFrame, kind: str) -> pd.DataFrame:
    old = pd.read_csv(path) if path.exists() else pd.DataFrame()
    if new.empty and old.empty:
        return old
    if not new.empty:
        new = _store_dates(new.copy())
    combined = pd.concat([f for f in (old, new) if not f.empty], ignore_index=True)
    combined["snapshot_date"] = combined["snapshot_date"].astype(str)
    # Newest export wins when the same day/video appears in more than one export.
    combined = combined.sort_values("snapshot_date", kind="stable")
    combined = combined.drop_duplicates(DEDUPE[kind], keep="last")
    if "date" in combined.columns:
        combined = combined.sort_values(["date"] + (["hour"] if "hour" in combined.columns else []),
                                        kind="stable")
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(path, index=False)
    return combined


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def fnum(x) -> str:
    return "" if pd.isna(x) else f"{int(round(x)):,}"


def short(s, n=70) -> str:
    s = re.sub(r"\s+", " ", str(s)).replace("|", "/").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(none)"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(x) else str(x) for x in r) + " |")
    return "\n".join(lines)


def split_caption(s, n: int):
    """Caption text without hashtags (shortened), and its hashtags."""
    s = str(s)
    return short(re.sub(r"#\w+", "", s), n), " ".join(re.findall(r"#\w+", s)[:6])


def vid_table(df: pd.DataFrame, show_len: bool, caption_len: int = 70) -> str:
    parts = df["title"].map(lambda s: split_caption(s, caption_len))
    t = pd.DataFrame({
        "posted": df["posted_at"].dt.strftime("%Y-%m-%d"),
        "caption": parts.map(lambda p: p[0]),
    })
    tags = parts.map(lambda p: p[1])
    if (tags != "").any():
        t["tags"] = tags
    t["views"] = df["views"].map(fnum)
    t["eng%"] = df["eng_rate"].round(1)
    if show_len:
        t["len_s"] = df["duration_s"].round(0)
    if df["avg_watch_s"].notna().any():
        t["avg_watch_s"] = df["avg_watch_s"].round(1)
    if df["full_watch_rate"].notna().any():
        t["full_watch%"] = df["full_watch_rate"].round(1)
    return md_table(t)


def group_table(v: pd.DataFrame, key: str, label: str, order=None) -> str:
    g = (v.dropna(subset=[key]).groupby(key)
         .agg(posts=("views", "size"), median_views=("views", "median"),
              median_eng_pct=("eng_rate", "median")))
    if order:
        g = g.reindex([o for o in order if o in g.index])
    g = g.reset_index().rename(columns={key: label})
    g["median_views"] = g["median_views"].map(fnum)
    g["median_eng_pct"] = g["median_eng_pct"].round(1)
    return md_table(g)


def time_bucket(h):
    if pd.isna(h):
        return None
    h = int(h)
    if 5 <= h <= 11:
        return "morning (5-11)"
    if 12 <= h <= 16:
        return "afternoon (12-16)"
    if 17 <= h <= 21:
        return "evening (17-21)"
    return "night (22-4)"


def dur_bucket(s):
    if pd.isna(s):
        return None
    return "<=15s" if s <= 15 else "16-30s" if s <= 30 else "31-60s" if s <= 60 else ">60s"


def hour_label(h) -> str:
    h = int(h)
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


# ----------------------------------------------------------------------------
# Analysis: per-video
# ----------------------------------------------------------------------------
def analyze_videos(hist: pd.DataFrame):
    h = hist.copy()
    h["posted_at"] = pd.to_datetime(h["posted_at"], errors="coerce")
    h["title"] = h["title"].fillna("")
    h = h.sort_values("snapshot_date", kind="stable")
    v = h.drop_duplicates("video_key", keep="last").copy()
    for k in ("likes", "comments", "shares", "saves"):
        v[k] = v[k].fillna(0)
    interactions = v[["likes", "comments", "shares", "saves"]].sum(axis=1)
    v["eng_rate"] = (interactions / v["views"] * 100).where(v["views"] > 0)
    v["hashtags"] = v["title"].str.count("#")
    v["caption_len"] = v["title"].str.len()
    v["weekday"] = v["posted_at"].dt.day_name()
    v["hour"] = v["posted_at"].dt.hour
    n = len(v)
    show_len = v["duration_s"].notna().any()
    out = []

    dated = v["posted_at"].dropna()
    span = f"{dated.min():%Y-%m-%d} to {dated.max():%Y-%m-%d}" if len(dated) else "unknown dates"
    out.append("## Overview\n" + "\n".join([
        f"- Videos analysed: {n} ({span})",
        f"- Total views: {fnum(v['views'].sum())}",
        f"- Median views per video: {fnum(v['views'].median())}",
        f"- Median engagement rate (likes+comments+shares+saves / views): {v['eng_rate'].median():.1f}%",
    ]))

    ranked = v.sort_values("views", ascending=False)
    k = min(5, n)
    out.append("## Top videos by views\n" + vid_table(ranked.head(k), show_len))
    if n > k:
        out.append("## Lowest videos by views\n" + vid_table(ranked.iloc[k:].tail(k), show_len))

    elig = v[v["views"] >= 50].sort_values("eng_rate", ascending=False)
    if len(elig):
        out.append("## Top videos by engagement rate (min 50 views)\n"
                   + vid_table(elig.head(5), show_len))

    if v["weekday"].notna().any():
        out.append("## By weekday posted\n" + group_table(v, "weekday", "weekday", WEEKDAYS))
    if v["hour"].notna().any() and (v["hour"].dropna() != 0).any():
        v["time_of_day"] = v["hour"].map(time_bucket)
        order = ["morning (5-11)", "afternoon (12-16)", "evening (17-21)", "night (22-4)"]
        out.append("## By time of day posted\n" + group_table(v, "time_of_day", "time_of_day", order))
    if show_len:
        v["length_bucket"] = v["duration_s"].map(dur_bucket)
        out.append("## By video length\n"
                   + group_table(v, "length_bucket", "length", ["<=15s", "16-30s", "31-60s", ">60s"]))

    if (v["title"] != "").any():
        v["has_question"] = v["title"].str.contains("?", regex=False).map(
            {True: "caption has '?'", False: "no '?' in caption"})
        v["hashtag_group"] = v["hashtags"].map(lambda x: "0-2 hashtags" if x <= 2 else "3+ hashtags")
        out.append("## Caption features\n" + group_table(v, "has_question", "caption")
                   + "\n\n" + group_table(v, "hashtag_group", "hashtags"))

    tag_lists = v["title"].map(lambda s: sorted({t.lower() for t in re.findall(r"#\w+", s)}))
    tagged = v.assign(tag=tag_lists).explode("tag").dropna(subset=["tag"])
    if len(tagged):
        g = (tagged.groupby("tag")
             .agg(videos=("views", "size"), median_views=("views", "median"),
                  median_eng_pct=("eng_rate", "median")))
        g = g[g["videos"] >= 2].sort_values(["videos", "median_views"], ascending=False).head(10)
        if len(g):
            g = g.reset_index()
            g["median_views"] = g["median_views"].map(fnum)
            g["median_eng_pct"] = g["median_eng_pct"].round(1)
            out.append("## Hashtags used on 2+ videos\n" + md_table(g))

    if n >= 8:
        lines = []
        for c, name in [("duration_s", "video length"), ("avg_watch_s", "avg watch time"),
                        ("hashtags", "hashtag count"), ("caption_len", "caption length")]:
            pair = v[["views", c]].dropna()
            if len(pair) >= 8 and pair[c].nunique() > 1:
                r = pair["views"].corr(pair[c], method="spearman")
                if pd.notna(r):
                    lines.append(f"- views vs {name}: rank correlation {r:+.2f} (n={len(pair)})")
        if lines:
            out.append("## Correlations with views (rank-based, weak evidence at small n)\n"
                       + "\n".join(lines))

    wk = v.dropna(subset=["posted_at"])
    if len(wk):
        g = (wk.groupby(wk["posted_at"].dt.to_period("W"))
             .agg(posts=("views", "size"), total_views=("views", "sum"), median_views=("views", "median"))
             .tail(8).reset_index().rename(columns={"posted_at": "week"}))
        g["week"] = g["week"].astype(str)
        g["total_views"] = g["total_views"].map(fnum)
        g["median_views"] = g["median_views"].map(fnum)
        out.append("## Posting cadence (last 8 weeks, by post date)\n" + md_table(g))

    snaps = sorted(h["snapshot_date"].unique())
    if len(snaps) >= 2:
        prev_d, last_d = snaps[-2], snaps[-1]
        a = h[h["snapshot_date"] == prev_d].drop_duplicates("video_key", keep="last").set_index("video_key")
        b = h[h["snapshot_date"] == last_d].drop_duplicates("video_key", keep="last").set_index("video_key")
        both = b[["views", "title"]].join(a[["views"]].rename(columns={"views": "before"}), how="inner")
        both["gained"] = both["views"] - both["before"]
        new_vids = len(b.index.difference(a.index))
        lines = [f"- Between exports {prev_d} and {last_d}: +{fnum(both['gained'].sum())} views "
                 f"on {len(both)} existing videos, {new_vids} new videos"]
        top = both.sort_values("gained", ascending=False).head(5)
        top = top[top["gained"] > 0]
        if len(top):
            t = pd.DataFrame({"caption": top["title"].map(short), "views_gained": top["gained"].map(fnum)})
            lines.append("\nBiggest gainers:\n\n" + md_table(t))
        out.append("## Growth since previous export\n" + "\n".join(lines))
    else:
        out.append("## Growth since previous export\n(only one export so far, so no week-over-week "
                   "change yet. Add another export next week.)")

    recent = v.sort_values("posted_at", ascending=False).head(40)
    out.append("## Recent posts (latest 40)\n" + vid_table(recent, show_len, caption_len=100))
    return "\n\n".join(out), n


# ----------------------------------------------------------------------------
# Analysis: daily series (Overview, Viewers, FollowerHistory)
# ----------------------------------------------------------------------------
def _prep_daily(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    return d.dropna(subset=["date"]).sort_values("date")


def _present(d: pd.DataFrame, cols: list) -> list:
    return [c for c in cols if c in d.columns and d[c].notna().any()]


def period_table(d: pd.DataFrame, metrics: list, days: int) -> str:
    last = d["date"].max()
    cur = d[d["date"] > last - pd.Timedelta(days=days)]
    prev = d[(d["date"] <= last - pd.Timedelta(days=days)) & (d["date"] > last - pd.Timedelta(days=2 * days))]
    rows = []
    for c in metrics:
        a, b = cur[c].sum(), prev[c].sum()
        change = f"{(a - b) / b * 100:+.0f}%" if len(prev) and b > 0 else "n/a"
        rows.append({"metric": c, f"last_{days}d": fnum(a),
                     f"prior_{days}d": fnum(b) if len(prev) else "n/a", "change": change})
    return md_table(pd.DataFrame(rows))


def weekly_table(d: pd.DataFrame, metrics: list, weeks: int = 8) -> str:
    w = d.set_index("date")[metrics].resample("W-SUN").sum(min_count=1).tail(weeks)
    w = w.reset_index().rename(columns={"date": "week_ending"})
    w["week_ending"] = w["week_ending"].dt.strftime("%Y-%m-%d")
    for c in metrics:
        w[c] = w[c].map(fnum)
    return md_table(w)


def activity_section(title: str, d: pd.DataFrame, metrics: list, extra=None) -> str:
    first, last = d["date"].min(), d["date"].max()
    lines = [f"- Covers {first:%Y-%m-%d} to {last:%Y-%m-%d} ({len(d)} days)"]
    active = d[(d[metrics].fillna(0) != 0).any(axis=1)] if metrics else d.iloc[0:0]
    if active.empty:
        lines.append("- No activity recorded in this window.")
    else:
        lines.append(f"- First day with any activity: {active['date'].min():%Y-%m-%d}")
    lines += extra or []
    out = f"## {title}\n" + "\n".join(lines)
    if not active.empty:
        out += "\n\n### Last 7 days vs the 7 before\n" + period_table(d, metrics, 7)
        if len(d) >= 56:
            out += "\n\n### Last 28 days vs the 28 before\n" + period_table(d, metrics, 28)
        out += "\n\n### Weekly totals (last 8 weeks)\n" + weekly_table(d, metrics)
    return out


def analyze_overview(daily: pd.DataFrame) -> str:
    d = _prep_daily(daily)
    metrics = _present(d, ["views", "profile_views", "likes", "comments", "shares"])
    extra = []
    if "views" in metrics and d["views"].fillna(0).sum() > 0:
        top = d.loc[d["views"].idxmax()]
        extra.append(f"- Total views in window: {fnum(d['views'].sum())}; "
                     f"best day: {top['date']:%Y-%m-%d} ({fnum(top['views'])} views)")
    return activity_section("Account overview (daily totals)", d, metrics, extra)


def analyze_viewers(viewers: pd.DataFrame) -> str:
    d = _prep_daily(viewers)
    metrics = _present(d, ["total_viewers", "new_viewers", "returning_viewers"])
    extra = []
    if "total_viewers" not in metrics:
        extra.append("- The Total Viewers column is empty in the export, so new and returning viewers are used.")
    return activity_section("Viewers", d, metrics, extra)


def analyze_follower_history(fh: pd.DataFrame) -> str:
    d = _prep_daily(fh)
    extra = []
    if d["followers"].notna().any():
        extra.append(f"- Latest follower count in export: {fnum(d['followers'].dropna().iloc[-1])}")
    else:
        extra.append("- The Followers column is empty in the export, so only daily changes are available.")
    if d["net_change"].notna().any():
        extra.append(f"- Net follower change over the whole window: {d['net_change'].sum():+,.0f}")
        best = d.loc[d["net_change"].idxmax()]
        if best["net_change"] > 0:
            extra.append(f"- Best day for new followers: {best['date']:%Y-%m-%d} (+{fnum(best['net_change'])})")
    return activity_section("Follower growth (daily net change)", d,
                            _present(d, ["net_change"]), extra)


def analyze_follower_activity(a: pd.DataFrame) -> str:
    d = a.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date", "hour", "active_followers"])
    title = "## When your followers are active"
    if d.empty or d["active_followers"].sum() == 0:
        return f"{title}\n(no active-follower activity recorded)"
    d["hour"] = d["hour"].astype(int)
    days = d["date"].nunique()
    by_hour = d.groupby("hour")["active_followers"].mean().reindex(range(24))
    top = by_hour.dropna().sort_values(ascending=False).head(5)
    filled = by_hour.fillna(0)
    windows = {h: sum(filled[(h + k) % 24] for k in range(3)) for h in range(24)}
    best = max(windows, key=windows.get)
    t = pd.DataFrame({"hour": [hour_label(h) for h in top.index],
                      "avg_active_followers": top.round(1).values})
    return (f"{title}\n"
            f"- Based on {days} day(s) of hourly data ({d['date'].min():%Y-%m-%d} to {d['date'].max():%Y-%m-%d}). "
            f"Hours are as exported, so confirm the time zone in TikTok Studio.\n"
            f"- Busiest 3-hour window: {hour_label(best)} to {hour_label((best + 3) % 24)}\n\n"
            f"Top hours:\n\n{md_table(t)}")


def analyze_distribution(df: pd.DataFrame, title: str) -> str:
    d = df.copy()
    d["snapshot_date"] = d["snapshot_date"].astype(str)
    latest = d["snapshot_date"].max()
    d = d[d["snapshot_date"] == latest].sort_values("share_pct", ascending=False)
    t = pd.DataFrame({"category": d["category"], "share_%": d["share_pct"].round(1)})
    return f"## {title} (export of {latest})\n" + md_table(t)


def build_stats(hist: dict):
    parts, n_videos = [], 0
    if not hist["video"].empty:
        sec, n_videos = analyze_videos(hist["video"])
        parts.append(sec)
    if not hist["daily"].empty:
        parts.append(analyze_overview(hist["daily"]))
    if not hist["viewers"].empty:
        parts.append(analyze_viewers(hist["viewers"]))
    if not hist["follower_history"].empty:
        parts.append(analyze_follower_history(hist["follower_history"]))
    if not hist["follower_activity"].empty:
        parts.append(analyze_follower_activity(hist["follower_activity"]))
    if not hist["gender"].empty:
        parts.append(analyze_distribution(hist["gender"], "Follower gender"))
    if not hist["territories"].empty:
        parts.append(analyze_distribution(hist["territories"], "Follower top territories"))
    return "\n\n".join(parts), n_videos


# ----------------------------------------------------------------------------
# Claude insights
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a TikTok growth analyst advising a small creator. Be specific and practical, "
    "be honest about uncertainty, and never invent data. Only use numbers that appear in the "
    "analytics you are given. If the data looks empty or broken, say so plainly instead of guessing."
)


def build_prompt(stats: str, n_videos: int, niche: str, goal: str) -> str:
    ctx = f"Creator niche: {niche or 'not specified'}\nCreator goal: {goal or 'grow views and followers'}\n\n"
    caveat = ""
    if n_videos == 0:
        caveat = ("There is no per-video data in this export, only account-level and audience data. "
                  "Do not invent video-level findings; say so briefly and base the report on the "
                  "account and audience numbers.\n\n")
        first = "1. What's working: what the account and audience numbers show (growth, active hours, audience mix)."
    else:
        first = "1. What's working: cite specific videos by caption snippet and their numbers."
        if n_videos < 20:
            caveat = (f"Sample size warning: there are only {n_videos} videos. Treat every pattern as a "
                      "hypothesis, say so explicitly, and favour proposing experiments over drawing "
                      "conclusions.\n\n")
    ask = (
        "Write a concise report (under 450 words) with these sections:\n"
        f"{first}\n"
        "2. What's underperforming, and a possible reason (flag it as a guess).\n"
        "3. Three experiments for next week: specific and testable.\n"
        "4. What to track or export next time to learn more.\n"
        "Avoid generic TikTok advice that isn't tied to this data."
    )
    return f"{ctx}{caveat}<analytics>\n{stats}\n</analytics>\n\n{ask}"


def get_insights(stats: str, n_videos: int, model: str, niche: str, goal: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(stats, n_videos, niche, goal)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-ai", action="store_true", help="skip the Claude step, write stats only")
    ap.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL))
    ap.add_argument("--input-dir", type=Path, default=ROOT / "input")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "output")
    args = ap.parse_args()

    for p in (args.input_dir, args.data_dir, args.output_dir):
        p.mkdir(parents=True, exist_ok=True)

    print(f"Reading exports from {args.input_dir} ...")
    new = load_inputs(args.input_dir)
    hist = {k: update_history(args.data_dir / fname, new[k], k) for k, fname in HISTORY_FILES.items()}

    if all(h.empty for h in hist.values()):
        print("\nNo usable data yet. Drop TikTok Studio exports into the input/ folder and run again.")
        return 1

    stats, n_videos = build_stats(hist)

    insights = None
    if args.no_ai:
        print("\n--no-ai set: skipping Claude insights.")
    elif not os.getenv("ANTHROPIC_API_KEY"):
        print("\nANTHROPIC_API_KEY is not set (add it to .env), so skipping Claude insights. "
              "Stats are still written.")
    else:
        print(f"\nAsking Claude ({args.model}) for insights ...")
        try:
            insights = get_insights(stats, n_videos, args.model,
                                    os.getenv("NICHE", ""), os.getenv("GOAL", ""))
        except Exception as e:  # noqa: BLE001
            print(f"Claude step failed ({type(e).__name__}: {e}). Stats are still written.")

    today = date.today().isoformat()
    report = f"# TikTok report, {today}\n\n"
    if insights:
        report += f"## Insights\n\n{insights}\n\n---\n\n"
    report += f"# Data summary\n\n{stats}\n"
    out_path = args.output_dir / f"report_{today}.md"
    out_path.write_text(report, encoding="utf-8")

    print(f"\n{'=' * 60}\n{insights if insights else 'No AI insights this run.'}\n{'=' * 60}")
    print(f"\nFull report saved to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
