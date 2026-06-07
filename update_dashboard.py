#!/usr/bin/env python3
"""
update_dashboard.py
───────────────────────────────────────────────────────────────────
Daily update script for the Creative Performance Dashboard.

Usage:
    python3 update_dashboard.py           # merge + push to GitHub
    python3 update_dashboard.py --no-push # merge only, skip git push

Drop new Ads Manager CSV exports into the raw/ folder, then run this.
The script will:
  1. Detect new creative CSV files (Age + Platform, Grasp + BeezB)
  2. Fetch purchase data from Amplitude (purchase_completed_success,
     grouped by utm_content = ad_id)
  3. Merge everything into files/creative_dashboard.html
  4. Push the updated HTML to GitHub Pages
  5. Move processed files to processed/
───────────────────────────────────────────────────────────────────
"""

import csv
import json
import re
import sys
import shutil
import subprocess
import collections
import hashlib
import base64
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

# ─── Amplitude credentials ────────────────────────────────────────────────────
AMP_API_KEY = "64cb613a6e1bf9df7c5483b8a4ac4bd6"
AMP_SECRET  = "2433f8892d713db9c049926a819525b3"
AMP_SEG_URL = "https://amplitude.com/api/2/events/segmentation"

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE     = Path(__file__).parent
RAW      = BASE / "raw"
PROC     = BASE / "processed"
MANIFEST = BASE / "data" / "dashboard_manifest.json"
DASHBOARD= BASE / "files" / "creative_dashboard.html"

PROC.mkdir(parents=True, exist_ok=True)
(BASE / "data").mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
def log(msg, indent=0):
    prefix = "  " * indent
    print(f"{prefix}{msg}")

def ok(msg,  indent=1): log(f"✓ {msg}", indent)
def err(msg, indent=1): log(f"✗ {msg}", indent)
def info(msg,indent=1): log(f"→ {msg}", indent)

# ─── Manifest (avoid re-processing same file twice) ───────────────────────────
def load_manifest():
    if MANIFEST.exists():
        return set(json.loads(MANIFEST.read_text()))
    return set()

def save_manifest(seen):
    MANIFEST.write_text(json.dumps(sorted(seen), indent=2))

def file_hash(path):
    return hashlib.md5(path.read_bytes()).hexdigest()

# ─── File classification ──────────────────────────────────────────────────────
def classify(path):
    """
    Returns (kind, agency) or (None, None) if not a recognised creative file.
    kind   : 'age' or 'platform'
    agency : 'Grasp' or 'BeezB'
    """
    name = path.name.lower()

    # Must be a creative file
    if "_creative" not in name:
        return None, None

    # Determine agency
    if "grasp" in name:
        agency = "Grasp"
    elif "beezb" in name:
        agency = "BeezB"
    else:
        return None, None   # unknown agency — skip

    # Determine kind
    if "type_age" in name:
        kind = "age"
    elif "type_platform" in name or "type_placement" in name:
        kind = "platform"
    else:
        return None, None

    return kind, agency

# ─── CSV helpers ──────────────────────────────────────────────────────────────
def pf(v):
    try:   return float(v) if v and str(v).strip() else 0.0
    except: return 0.0

def pi(v):
    try:   return int(float(v)) if v and str(v).strip() else 0
    except: return 0

def parse_keys(name):
    keys = {}
    for part in name.split("_"):
        if ":" in part:
            k, v = part.split(":", 1)
            keys[k.upper()] = v
    return keys

def hook_col(agency):
    return "3 sec hook"

def hold_col(agency):
    return "Hold rate" if agency == "BeezB" else "Hold Rate"

def blank_acc():
    return {
        "spend": 0, "imp": 0, "reach": 0, "clk": 0,
        "pur": 0, "reg": 0, "rev": 0, "apps": 0,
        "hook_w": 0, "hook_imp": 0, "hold_w": 0, "hold_imp": 0,
        "name": "", "agency": "", "des": "", "typ": "",
        "ver": "", "utrcncp": "", "s3": "", "v3s": "",
    }

def accumulate(d, row, agency):
    sp  = pf(row.get("Amount spent (USD)", 0))
    imp = pi(row.get("Impressions", 0))
    d["spend"]  += sp
    d["imp"]    += imp
    d["reach"]  += pi(row.get("Reach", 0))
    d["clk"]    += pi(row.get("Link clicks", 0))
    d["pur"]    += pi(row.get("Purchases", 0))
    d["reg"]    += pi(row.get("Registrations completed", 0))
    d["rev"]    += pf(row.get("Purchases conversion value", 0))
    d["apps"]   += pi(row.get("Applications submitted", 0))
    hk = pf(row.get(hook_col(agency), 0))
    hl = pf(row.get(hold_col(agency), 0))
    if hk > 0 and imp > 0:
        d["hook_w"]   += hk * imp
        d["hook_imp"] += imp
    if hl > 0 and imp > 0:
        d["hold_w"]   += hl * imp
        d["hold_imp"] += imp

def compute_metrics(d, label_key, label_val):
    sp  = d["spend"]; imp = d["imp"]; clk = d["clk"]
    pur = d["pur"];   reg = d["reg"]
    hook = round(d["hook_w"] / d["hook_imp"], 4) if d["hook_imp"] > 0 else None
    hold = round(d["hold_w"] / d["hold_imp"], 4) if d["hold_imp"] > 0 else None
    return {
        label_key: label_val,
        "spend":  round(sp, 2),
        "imp":    imp,
        "reach":  d["reach"],
        "clk":    clk,
        "pur":    pur,
        "reg":    reg,
        "rev":    round(d["rev"], 2),
        "cpm":    round(sp / imp * 1000, 2) if imp else None,
        "ctr":    round(clk / imp * 100,  3) if imp else None,
        "cpp":    round(sp / pur,          2) if pur else None,
        "cpreg":  round(sp / reg,          2) if reg else None,
        "r_lc":   round(reg / clk,         4) if clk else None,
        "p_reg":  round(pur / reg,         4) if reg else None,
        "p_lc":   round(pur / clk,         4) if clk else None,
        "hook":   hook,
        "hold":   hold,
        "sp_share": None,
    }

# ─── Ingest a single creative file ────────────────────────────────────────────
def ingest_file(path, kind, agency, ads_acc, age_acc, place_acc, daily_acc,
                accumulate_totals=True):
    """Read one CSV and accumulate its rows into the four dictionaries.

    accumulate_totals: when False, skip ads_acc and daily_acc (used for the
    platform/placement file when an age file already covers totals for the
    same agency, to prevent double-counting identical spend).
    """
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        aid  = row.get("Ad ID", "").strip()
        name = row.get("Ad name", "").strip()
        day  = row.get("Day", "").strip()
        keys = parse_keys(name)

        # ── per-ad totals and daily timeseries ────────────────────────────
        # Only accumulate from ONE file type per agency to avoid double-counting.
        # Age files are preferred; platform files are skipped for totals when an
        # age file for the same agency was already processed.
        if accumulate_totals:
            if aid not in ads_acc:
                ads_acc[aid] = blank_acc()
                ads_acc[aid].update({
                    "name": name, "agency": agency,
                    "des": keys.get("DES", ""), "typ": keys.get("TYP", ""),
                    "ver": keys.get("VER", ""), "utrcncp": keys.get("UTRCNCP", ""),
                    "s3":  keys.get("3S", ""),  "v3s": keys.get("V3S", ""),
                })
            accumulate(ads_acc[aid], row, agency)

            dk = (aid, day)
            if dk not in daily_acc:
                daily_acc[dk] = blank_acc()
            accumulate(daily_acc[dk], row, agency)

        # ── per-(ad, age) or per-(ad, placement) ──────────────────────────
        if kind == "age":
            age = row.get("Age", "").strip()
            ak  = (aid, age)
            if ak not in age_acc:
                age_acc[ak] = blank_acc()
            accumulate(age_acc[ak], row, agency)
        else:
            place = row.get("Placement", "").strip()
            pk    = (aid, place)
            if pk not in place_acc:
                place_acc[pk] = blank_acc()
            accumulate(place_acc[pk], row, agency)

# ─── Merge new rows into existing array ───────────────────────────────────────
def merge_into(existing, new_rows, id_keys):
    """
    Merge new_rows into existing list.
    For NEW keys: append the row.
    For EXISTING keys: only update if the incoming data covers a genuinely
    new time period — detected by checking whether the new row's day/date
    key is already represented.  For non-daily aggregates (ads, age, place)
    we SKIP updates to avoid double-counting when the same file is processed
    twice; those aggregates are rebuilt from scratch by the rebuild path.
    """
    def make_key(r):
        return tuple(r[k] for k in id_keys)

    idx    = {make_key(r): i for i, r in enumerate(existing)}
    merged = list(existing)

    for n in new_rows:
        k = make_key(n)
        if k in idx:
            # Key already exists — skip to avoid double-counting.
            # The rebuild_from_scratch path handles re-aggregation when needed.
            pass
        else:
            merged.append(n)
            idx[k] = len(merged) - 1

    return merged

# ─── Amplitude: fetch purchase counts + revenue grouped by utm_content ────────
def fetch_amplitude_data(date_from, date_to):
    """
    Query Amplitude for purchase_completed_success events grouped by utm_content.
    Returns (totals, daily) where:
      totals: { ad_id: {"amp_pur": int, "amp_rev": float} }
      daily:  { ad_id: { "YYYY-MM-DD": int } }
    """
    from datetime import date as _date, timedelta as _td
    start = date_from.replace("-", "")
    end   = date_to.replace("-", "")
    creds = base64.b64encode(f"{AMP_API_KEY}:{AMP_SECRET}".encode()).decode()
    hdrs  = {"Authorization": f"Basic {creds}"}

    def amp_get(params):
        url = AMP_SEG_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            err(f"Amplitude API error: {e}")
            return None

    log("\nFetching Amplitude data…")
    resp = amp_get({
        "e":     json.dumps({"event_type": "purchase_completed_success"}),
        "m":     "uniques",
        "g":     "gp:utm_content",
        "start": start, "end": end,
        "i":     1,
        "limit": 100,
    })

    totals = {}
    daily  = {}
    if resp and "data" in resp:
        labels   = resp["data"].get("seriesLabels", [])
        series   = resp["data"].get("series",       [])
        start_dt = _date.fromisoformat(date_from)
        for label, vals in zip(labels, series):
            if not label or label == "(none)":
                continue
            total = int(sum(vals))
            if total > 0:
                totals[str(label)] = {"amp_pur": total, "amp_rev": 0.0}
            day_map = {}
            for i, v in enumerate(vals):
                if v > 0:
                    day_map[(_date.fromisoformat(date_from) + _td(days=i)).isoformat()] = int(v)
            if day_map:
                daily[str(label)] = day_map
        ok(f"Amplitude purchases: {sum(v['amp_pur'] for v in totals.values())} "
           f"total across {len(totals)} ads")
    else:
        info("Amplitude purchase count unavailable — amp_pur will be 0")

    return totals, daily


# ─── Rebuild RAW_ADS totals from all RAW_DAILY rows ───────────────────────────
def rebuild_ads_from_daily(daily_rows, meta_map):
    """
    Recompute per-ad totals by summing every RAW_DAILY row for each ad.
    meta_map: dict of ad_id → existing ad dict (for name/agency/des/etc).
    This ensures RAW_ADS is always consistent with RAW_DAILY regardless of
    which days were processed in which update run.
    """
    acc = {}
    for row in daily_rows:
        aid = row["ad_id"]
        if aid not in acc:
            acc[aid] = dict(spend=0,imp=0,clk=0,pur=0,reg=0,rev=0,hw=0.0,hi=0,ldw=0.0,li=0)
        d   = acc[aid]
        imp = row.get("imp") or 0
        hk  = row.get("hook")
        hl  = row.get("hold")
        d["spend"] += row.get("spend") or 0
        d["imp"]   += imp
        d["clk"]   += row.get("clk")  or 0
        d["pur"]   += row.get("pur")  or 0
        d["reg"]   += row.get("reg")  or 0
        d["rev"]   += row.get("rev")  or 0
        if hk is not None and imp > 0:
            d["hw"] += hk * imp; d["hi"] += imp
        if hl is not None and imp > 0:
            d["ldw"] += hl * imp; d["li"] += imp

    result = []
    for aid, d in acc.items():
        meta = meta_map.get(aid, {})
        sp=d["spend"]; imp=d["imp"]; clk=d["clk"]; pur=d["pur"]; reg=d["reg"]
        hook = round(d["hw"]/d["hi"],4)   if d["hi"] else None
        hold = round(d["ldw"]/d["li"],4)  if d["li"] else None
        result.append({
            "id": aid,
            "spend": round(sp,2), "imp": imp, "clk": clk, "pur": pur, "reg": reg,
            "rev": round(d["rev"],2),
            "cpm":   round(sp/imp*1000,2) if imp else None,
            "ctr":   round(clk/imp*100,3) if imp else None,
            "cpp":   round(sp/pur,2)      if pur else None,
            "cpreg": round(sp/reg,2)      if reg else None,
            "r_lc":  round(reg/clk,4)    if clk else None,
            "p_reg": round(pur/reg,4)    if reg else None,
            "p_lc":  round(pur/clk,4)   if clk else None,
            "hook": hook, "hold": hold, "sp_share": None,
            "name":      meta.get("name",""),
            "agency":    meta.get("agency",""),
            "des":       meta.get("des",""),
            "typ":       meta.get("typ",""),
            "ver":       meta.get("ver",""),
            "utrcncp":   meta.get("utrcncp",""),
            "s3":        meta.get("s3",""),
            "v3s":       meta.get("v3s",""),
            "creative_url": meta.get("creative_url",""),
            "amp_pur":   0,
            "amp_rev":   0.0,
            "amp_cpp":   None,
        })
    return result

# ─── Rebuild RAW_DES from RAW_ADS ─────────────────────────────────────────────
def rebuild_des(ads):
    dm = collections.defaultdict(lambda: {
        "spend":0,"imp":0,"clk":0,"pur":0,"reg":0,"rev":0,
        "amp_pur":0,"amp_rev":0.0,
        "hw":0,"hi":0,"ldw":0,"li":0,"agencies":set(),
    })
    for a in ads:
        d = dm[a["des"]]
        for f in ["spend","imp","clk","pur","reg","rev"]:
            d[f] += a.get(f) or 0
        d["amp_pur"] += a.get("amp_pur") or 0
        d["amp_rev"] += a.get("amp_rev") or 0.0
        d["agencies"].add(a["agency"])
        if a.get("hook") and a.get("imp"):
            d["hw"] += a["hook"] * a["imp"]; d["hi"] += a["imp"]
        if a.get("hold") and a.get("imp"):
            d["ldw"] += a["hold"] * a["imp"]; d["li"] += a["imp"]

    result = []
    for des, d in dm.items():
        sp=d["spend"]; imp=d["imp"]; clk=d["clk"]; pur=d["pur"]; reg=d["reg"]
        amp_pur=d["amp_pur"]
        result.append({
            "des": des, "spend": round(sp,2), "imp": imp, "clk": clk,
            "pur": pur, "reg": reg, "rev": round(d["rev"],2),
            "amp_pur": amp_pur, "amp_rev": round(d["amp_rev"],2),
            "amp_cpp": round(sp/amp_pur,2) if amp_pur else None,
            "cpm":   round(sp/imp*1000,2) if imp else None,
            "ctr":   round(clk/imp*100,3) if imp else None,
            "cpp":   round(sp/pur,2)       if pur else None,
            "cpreg": round(sp/reg,2)       if reg else None,
            "r_lc":  round(reg/clk,4)      if clk else None,
            "p_reg": round(pur/reg,4)      if reg else None,
            "p_lc":  round(pur/clk,4)      if clk else None,
            "hook":  round(d["hw"]/d["hi"],4)   if d["hi"] else None,
            "hold":  round(d["ldw"]/d["li"],4)  if d["li"] else None,
            "agencies":  sorted(d["agencies"]),
            "ad_count":  sum(1 for a in ads if a["des"] == des),
        })
    result.sort(key=lambda x: -(x["spend"] or 0))
    return result

# ─── Patch the dashboard HTML ──────────────────────────────────────────────────
def extract_array(html, var):
    m = re.search(r"const\s+" + var + r"\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        raise ValueError(f"Cannot find {var} in dashboard HTML")
    return json.loads(m.group(1))

def replace_array(html, var, data):
    j = json.dumps(data, separators=(",", ":"))
    new_html, n = re.subn(
        r"(const\s+" + var + r"\s*=\s*)\[.*?\];",
        r"\g<1>" + j + ";",
        html, count=1, flags=re.DOTALL,
    )
    if n == 0:
        raise ValueError(f"Cannot replace {var}")
    return new_html

def parse_js_array(raw):
    """Parse a JS array string with single or double quotes into a Python list."""
    # normalise single-quoted strings → double-quoted so json.loads works
    normalised = re.sub(r"'([^']*)'", r'"\1"', raw)
    return json.loads(normalised)

def fmt_day(d):
    """'2026-06-01' → 'Jun 1'"""
    from datetime import date
    dt = date.fromisoformat(d)
    # %-d removes leading zero on Linux/Mac; use %#d on Windows if needed
    try:
        return dt.strftime("%b %-d")
    except ValueError:
        return dt.strftime("%b %d").lstrip("0").replace(" 0", " ")

def update_day_arrays(html, new_days):
    """Add any new date strings to DAYS and DAY_LBLS arrays in the JS."""
    m = re.search(r"const DAYS\s*=\s*(\[.*?\]);", html)
    if not m:
        return html
    current = parse_js_array(m.group(1))
    added   = [d for d in sorted(new_days) if d not in current]
    if not added:
        return html
    updated = current + added
    html = re.sub(r"const DAYS\s*=\s*\[.*?\];",
                  "const DAYS      = " + json.dumps(updated) + ";", html)
    m2 = re.search(r"const DAY_LBLS\s*=\s*(\[.*?\]);", html)
    if m2:
        cur_lbls = parse_js_array(m2.group(1))
        new_lbls = cur_lbls + [fmt_day(d) for d in added]
        html = re.sub(r"const DAY_LBLS\s*=\s*\[.*?\];",
                      "const DAY_LBLS  = " + json.dumps(new_lbls) + ";", html)
    return html

def update_date_picker(html, all_days):
    """Update date picker min/max/value attributes and JS state to match data range."""
    days_sorted = sorted(all_days)
    first = days_sorted[0]
    last  = days_sorted[-1]
    # date-from input: update max
    html = re.sub(
        r'(id="date-from"[^>]*max=")[^"]*(")',
        r'\g<1>' + last + r'\2', html
    )
    # date-to input: update value and max
    html = re.sub(
        r'(id="date-to"[^>]*value=")[^"]*(")',
        r'\g<1>' + last + r'\2', html
    )
    html = re.sub(
        r'(id="date-to"[^>]*max=")[^"]*(")',
        r'\g<1>' + last + r'\2', html
    )
    # "All" preset button data-to
    html = re.sub(
        r'(class="date-preset active"[^>]*data-from="[^"]*"\s*data-to=")[^"]*(")',
        r'\g<1>' + last + r'\2', html
    )
    # "Latest day" preset button — update data-from, data-to, and label text
    html = re.sub(
        r'(data-role="latest"[^>]*data-from=")[^"]*("[^>]*data-to=")[^"]*(")',
        r'\g<1>' + last + r'\2' + last + r'\3', html
    )
    # Also update the button's visible text label (between > and <)
    m = re.search(r'data-role="latest"[^>]*>([^<]+)<', html)
    if m:
        new_lbl = fmt_day(last)
        html = html[:m.start(1)] + new_lbl + html[m.end(1):]
    # "7d" and "3d" preset buttons — always relative to the last data day
    from datetime import date as _date, timedelta as _td
    last_date = _date.fromisoformat(last)
    d7_from = (last_date - _td(days=6)).isoformat()
    d3_from = (last_date - _td(days=2)).isoformat()
    html = re.sub(
        r'(<button class="date-preset"[^>]*data-from=")[^"]*("[^>]*data-to=")[^"]*("[^>]*>7d<)',
        r'\g<1>' + d7_from + r'\2' + last + r'\3', html
    )
    html = re.sub(
        r'(<button class="date-preset"[^>]*data-from=")[^"]*("[^>]*data-to=")[^"]*("[^>]*>3d<)',
        r'\g<1>' + d3_from + r'\2' + last + r'\3', html
    )
    # JS initial state
    html = re.sub(r"(dateFrom:\s*')[^']*(')", r'\g<1>' + first + r'\2', html)
    html = re.sub(r"(dateTo:\s*')[^']*(')",   r'\g<1>' + last  + r'\2', html)
    return html

def update_subtitle(html, all_days):
    """Update the subtitle date range string in the header."""
    from datetime import date
    days_sorted = sorted(all_days)
    first = date.fromisoformat(days_sorted[0])
    last  = date.fromisoformat(days_sorted[-1])
    if first.month == last.month and first.year == last.year:
        rng = f"{first.strftime('%b %-d')}–{last.day}, {last.year}"
    else:
        rng = f"{fmt_day(days_sorted[0])}–{fmt_day(days_sorted[-1])}, {last.year}"
    # replace any existing date range in the subtitle span
    html = re.sub(
        r"(May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Jan|Feb|Mar|Apr)\s+\d+[–\-].+?,\s*20\d\d",
        rng, html
    )
    return html

# ─── Git push ─────────────────────────────────────────────────────────────────
def git_push(date_label):
    log("Pushing to GitHub Pages…")
    try:
        subprocess.run(
            ["git", "-C", str(BASE), "add", "files/creative_dashboard.html"],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(BASE), "commit",
             "-m", f"Update dashboard — {date_label}"],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(BASE), "push"],
            check=True, capture_output=True
        )
        ok("Pushed → https://alex-sorku.github.io/nyk-creative-dashboard/")
    except subprocess.CalledProcessError as e:
        err(f"Git error: {e.stderr.decode().strip() if e.stderr else e}")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    push = "--no-push" not in sys.argv

    log("=" * 56)
    log("Creative Dashboard Update")
    log(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log("=" * 56)

    # 1. Find new creative CSVs in raw/
    manifest = load_manifest()
    new_files = []
    for path in sorted(RAW.glob("*.csv")):
        h = file_hash(path)
        if h in manifest:
            continue
        kind, agency = classify(path)
        if kind is None:
            info(f"Skipping (not a creative file): {path.name}")
            continue
        new_files.append((path, kind, agency, h))

    if not new_files:
        log("\nNo new creative CSV files found in raw/")
        log(f"Drop files into:  {RAW}")
        return

    log(f"\nFound {len(new_files)} new file(s):")
    for path, kind, agency, _ in new_files:
        info(f"{path.name}  [{kind} / {agency}]")

    # 2. Load existing dashboard data
    log("\nLoading existing dashboard…")
    if not DASHBOARD.exists():
        err(f"Dashboard not found: {DASHBOARD}")
        sys.exit(1)

    html = DASHBOARD.read_text(encoding="utf-8")
    existing_ads   = extract_array(html, "RAW_ADS")
    existing_des   = extract_array(html, "RAW_DES")
    existing_age   = extract_array(html, "RAW_AGE")
    existing_place = extract_array(html, "RAW_PLACE")
    existing_daily = extract_array(html, "RAW_DAILY")
    ok(f"ads={len(existing_ads)}, age={len(existing_age)}, "
       f"place={len(existing_place)}, daily={len(existing_daily)}")

    # 3. Aggregate new data
    log("\nIngesting new files…")
    ads_acc   = {}   # aid → accumulator
    age_acc   = {}   # (aid, age) → accumulator
    place_acc = {}   # (aid, place) → accumulator
    daily_acc = {}   # (aid, day) → accumulator
    new_days  = set()

    # Agencies that have an age file — their platform file should NOT re-accumulate totals
    agencies_with_age = {agency for _, kind, agency, _ in new_files if kind == "age"}

    for path, kind, agency, h in new_files:
        # Use age file for totals; skip totals from platform file if age file exists
        accumulate_totals = (kind == "age") or (agency not in agencies_with_age)
        ingest_file(path, kind, agency, ads_acc, age_acc, place_acc, daily_acc,
                    accumulate_totals)
        # collect new day values from filename or data
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d = row.get("Day","").strip()
                if d: new_days.add(d)
        ok(f"{path.name}")

    # Build computed rows from accumulators
    new_ads_rows = []
    for aid, d in ads_acc.items():
        r = compute_metrics(d, "id", aid)
        r.update({"name":d["name"],"agency":d["agency"],"des":d["des"],
                  "typ":d["typ"],"ver":d["ver"],"utrcncp":d["utrcncp"],
                  "s3":d["s3"],"v3s":d["v3s"]})
        new_ads_rows.append(r)

    new_age_rows   = [{"ad_id":k[0],"age":k[1],  **compute_metrics(d,"age",k[1])}
                      for k,d in age_acc.items()]
    new_place_rows = [{"ad_id":k[0],"place":k[1],**compute_metrics(d,"place",k[1])}
                      for k,d in place_acc.items()]
    new_daily_rows = [{"ad_id":k[0],"day":k[1],  **compute_metrics(d,"day",k[1])}
                      for k,d in daily_acc.items()]

    # 4. Merge into existing arrays
    log("\nMerging data…")
    # Age, place, daily: merge by key (append new keys only)
    merged_age   = merge_into(existing_age,   new_age_rows,   ["ad_id","age"])
    merged_place = merge_into(existing_place, new_place_rows, ["ad_id","place"])
    merged_daily = merge_into(existing_daily, new_daily_rows, ["ad_id","day"])

    # Ads: rebuild totals from all RAW_DAILY to keep in sync across update runs.
    # Metadata (name, agency, des, etc.) comes from existing_ads + new_ads_rows.
    meta_map = {a["id"]: a for a in existing_ads}
    for r in new_ads_rows:
        if r["id"] not in meta_map:
            meta_map[r["id"]] = r  # register new ads
    merged_ads = rebuild_ads_from_daily(merged_daily, meta_map)

    ok(f"ads={len(merged_ads)} (+{len(merged_ads)-len(existing_ads)}), "
       f"age={len(merged_age)} (+{len(merged_age)-len(existing_age)}), "
       f"place={len(merged_place)} (+{len(merged_place)-len(existing_place)}), "
       f"daily={len(merged_daily)} (+{len(merged_daily)-len(existing_daily)})")

    # 4b. Fetch Amplitude purchase data and inject into ads
    all_days_sorted = sorted(set(r["day"] for r in merged_daily))
    amp_date_from   = all_days_sorted[0]  if all_days_sorted else "2026-05-22"
    amp_date_to     = all_days_sorted[-1] if all_days_sorted else datetime.now().strftime("%Y-%m-%d")
    amp_totals, amp_daily = fetch_amplitude_data(amp_date_from, amp_date_to)

    amp_injected = 0
    for ad in merged_ads:
        amp = amp_totals.get(ad["id"], {})
        ad["amp_pur"] = amp.get("amp_pur", 0)
        ad["amp_rev"] = amp.get("amp_rev", 0.0)
        ad["amp_cpp"] = round(ad["spend"] / ad["amp_pur"], 2) if ad.get("amp_pur") else None
        if ad["amp_pur"]:
            amp_injected += 1
    ok(f"Amplitude data injected into {amp_injected}/{len(merged_ads)} ads")

    # Inject daily amp_pur into merged_daily rows (used by charts)
    daily_inj = 0
    for row in merged_daily:
        v = amp_daily.get(row["ad_id"], {}).get(row["day"], 0)
        row["amp_pur"] = v
        row["amp_cpp"] = round(row["spend"] / v, 2) if v and row.get("spend") else None
        if v: daily_inj += 1
    ok(f"Daily amp_pur injected into {daily_inj}/{len(merged_daily)} daily rows")

    # Save Amplitude snapshot for reference
    amp_snap_path = BASE / "data" / "amplitude_data.json"
    amp_snap_path.write_text(json.dumps(amp_totals, indent=2))
    ok(f"Amplitude snapshot → data/amplitude_data.json ({len(amp_totals)} entries)")

    merged_des = rebuild_des(merged_ads)

    # 5. Patch dashboard HTML
    log("\nPatching dashboard HTML…")
    for var, data in [
        ("RAW_ADS",   merged_ads),
        ("RAW_DES",   merged_des),
        ("RAW_AGE",   merged_age),
        ("RAW_PLACE", merged_place),
        ("RAW_DAILY", merged_daily),
    ]:
        html = replace_array(html, var, data)

    # Update chart day arrays, subtitle, and date picker bounds
    all_days = set(r["day"] for r in merged_daily)
    html = update_day_arrays(html, new_days)
    html = update_subtitle(html, all_days)
    html = update_date_picker(html, all_days)

    DASHBOARD.write_text(html, encoding="utf-8")
    ok(f"Saved → {DASHBOARD.name}  ({len(html)//1024} KB)")

    # 6. Move processed files + update manifest
    for path, kind, agency, h in new_files:
        dest = PROC / path.name
        if dest.exists():
            dest = PROC / (path.stem + f"_dup{path.suffix}")
        shutil.move(str(path), str(dest))
        manifest.add(h)

    save_manifest(manifest)
    ok(f"Moved {len(new_files)} file(s) to processed/")

    # 7. Git push
    if push:
        date_label = sorted(new_days)[-1] if new_days else datetime.now().strftime("%Y-%m-%d")
        log("")
        git_push(date_label)
    else:
        log("\n[--no-push] Skipping git push.")

    log("\n" + "=" * 56)
    log("Done!")
    if push:
        log("  Live at: https://alex-sorku.github.io/nyk-creative-dashboard/")
    log("=" * 56)


if __name__ == "__main__":
    main()
