#!/usr/bin/env python3
"""
inject_amplitude_diagnosis.py
──────────────────────────────────────────────────────────────────
Fetches Amplitude purchase_completed_success uniques per period and
injects amp_pur, CPA_amp_pur, and updated CVR_ra/CVR_ca metrics into
GraspAdAccount_Performance_Diagnosis.html D data object.

Also computes "yesterday" period data from creative_dashboard.html
RAW_DAILY and Amplitude.

Run after updating CSVs or whenever you want fresh Amplitude data.
──────────────────────────────────────────────────────────────────
"""

import json, re, sys, base64, urllib.request, urllib.parse, calendar
import datetime as dt
from pathlib import Path
import subprocess

BASE  = Path(__file__).parent
DIAG  = BASE / "GraspAdAccount_Performance_Diagnosis.html"
DASH  = BASE / "files" / "creative_dashboard.html"

AMP_API_KEY = "64cb613a6e1bf9df7c5483b8a4ac4bd6"
AMP_SECRET  = "2433f8892d713db9c049926a819525b3"
AMP_SEG_URL = "https://amplitude.com/api/2/events/segmentation"

push = "--no-push" not in sys.argv

def log(m):  print(m)
def ok(m):   print(f"  ✓ {m}")
def info(m): print(f"  → {m}")
def err(m):  print(f"  ✗ {m}")

MONTH_MAP = {
    'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
    'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12
}

def period_to_dates(pk, yesterday):
    """Return (date_from, date_to) ISO strings for a period key, or (None, None)."""
    if pk == 'yesterday':
        s = yesterday.isoformat()
        return s, s
    if pk == 'last_3d':
        d_from = yesterday - dt.timedelta(days=2)
        return d_from.isoformat(), yesterday.isoformat()
    for mo, mn in MONTH_MAP.items():
        if pk.startswith(mo + ' '):
            yr_raw = pk[len(mo)+1:len(mo)+3]
            try:
                yr = int('20' + yr_raw)
            except ValueError:
                continue
            first = dt.date(yr, mn, 1)
            if yr == yesterday.year and mn == yesterday.month:
                return first.isoformat(), yesterday.isoformat()
            last_day = calendar.monthrange(yr, mn)[1]
            return first.isoformat(), dt.date(yr, mn, last_day).isoformat()
    return None, None

def fetch_amp_total(date_from, date_to):
    """Total unique purchase_completed_success for the date range (no group-by)."""
    creds = base64.b64encode(f"{AMP_API_KEY}:{AMP_SECRET}".encode()).decode()
    hdrs  = {"Authorization": f"Basic {creds}"}
    params = {
        "e":     json.dumps({"event_type": "purchase_completed_success"}),
        "m":     "uniques",
        "start": date_from.replace("-", ""),
        "end":   date_to.replace("-", ""),
        "i":     1,
    }
    url = AMP_SEG_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except Exception as e:
        err(f"Amplitude API error for {date_from}→{date_to}: {e}")
        return 0
    if resp and "data" in resp:
        series = resp["data"].get("series", [])
        if series and series[0]:
            return int(sum(series[0]))
    return 0

def extract_json(html, var):
    m = re.search(r'const\s+' + var + r'\s*=\s*(\{.*?\}|\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError(f"Cannot find {var}")
    return json.loads(m.group(1))

def replace_json(html, var, data):
    j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    new_html, n = re.subn(
        r'(const\s+' + var + r'\s*=\s*)(\{.*?\}|\[.*?\]);',
        r'\g<1>' + j + ';',
        html, count=1, flags=re.DOTALL,
    )
    if n == 0:
        raise ValueError(f"Cannot replace {var}")
    return new_html

# ── Load diagnosis HTML ────────────────────────────────────────────────────────
log("=" * 60)
log("Amplitude injection → GraspAdAccount_Performance_Diagnosis.html")
log("=" * 60)

html = DIAG.read_text(encoding="utf-8")
D    = extract_json(html, "D")
ok(f"Loaded D with periods: {D['period_order']}")

yesterday = dt.date.today() - dt.timedelta(days=1)
info(f"Today: {dt.date.today()}, Yesterday: {yesterday}")

# ── Fetch amp_pur per period ───────────────────────────────────────────────────
log("\nFetching Amplitude totals per period…")
for pk in list(D['period_order']) + ['yesterday']:
    d_from, d_to = period_to_dates(pk, yesterday)
    if d_from is None:
        info(f"  Skip {pk!r} — cannot parse date range")
        continue
    amp = fetch_amp_total(d_from, d_to)
    info(f"  {pk}: {d_from} → {d_to}  amp_pur={amp}")

    if pk not in D['period_totals']:
        D['period_totals'][pk] = {}
    m = D['period_totals'][pk]
    m['amp_pur'] = amp

    sp   = m.get('spend')  or 0
    rg   = m.get('regs')   or 0
    lc   = m.get('link_clicks') or 0
    m['CPA_amp_pur'] = round(sp / amp, 2) if amp else None
    m['CVR_ra'] = round(amp / rg  * 100, 4) if rg  else None  # Reg→Amp Pur
    m['CVR_ca'] = round(amp / lc  * 100, 4) if lc  else None  # Clk→Amp Pur

# ── Build yesterday period ─────────────────────────────────────────────────────
log("\nBuilding yesterday period from RAW_DAILY…")
yk = 'yesterday'
ystr = yesterday.isoformat()

try:
    dash_html = DASH.read_text(encoding="utf-8")
    raw_daily = extract_json(dash_html, "RAW_DAILY")
    yrows = [r for r in raw_daily if r.get("day") == ystr]

    sp=0; imp=0; clk=0; pur=0; reg=0
    for r in yrows:
        sp  += r.get("spend") or 0
        imp += r.get("imp")   or 0
        clk += r.get("clk")   or 0
        pur += r.get("pur")   or 0
        reg += r.get("reg")   or 0

    ok(f"Yesterday from {len(yrows)} RAW_DAILY rows: spend=${sp:.2f} imp={imp} clk={clk} pur={pur} reg={reg}")

    m = D['period_totals'].get(yk, {})
    amp = m.get('amp_pur', 0)

    D['period_totals'][yk] = {
        "spend":       round(sp, 2),
        "impressions": imp,
        "reach":       imp,        # reach not available in RAW_DAILY; use imp as proxy
        "link_clicks": clk,
        "apps":        pur,        # Meta "apps" ~ purchases in this context
        "regs":        reg,
        "amp_pur":     amp,
        "CPM":         round(sp/imp*1000, 2) if imp  else None,
        "CPP":         round(sp/pur,      2) if pur  else None,
        "CTR":         round(clk/imp*100,  4) if imp  else None,
        "CPC":         round(sp/clk,       2) if clk  else None,
        "CPA_app":     round(sp/pur,       2) if pur  else None,
        "CPA_reg":     round(sp/reg,       2) if reg  else None,
        "CPA_amp_pur": round(sp/amp,       2) if amp  else None,
        "CVR_cr":      round(reg/clk*100,  4) if clk  else None,
        "CVR_ra":      round(amp/reg*100,  4) if reg  else None,
        "CVR_ca":      round(amp/clk*100,  4) if clk  else None,
    }
except Exception as e:
    err(f"Could not load RAW_DAILY from dashboard: {e}")
    info("Yesterday meta data will be zeroed (amp_pur already set)")

# ── Update period_order and period_labels ──────────────────────────────────────
if yk not in D['period_order']:
    D['period_order'].append(yk)
    ok("Added 'yesterday' to period_order")

if yk not in D['period_labels']:
    D['period_labels'][yk] = f"Yesterday ({yesterday.strftime('%d %b')})"
    ok(f"Added period label: {D['period_labels'][yk]}")

# ── Patch HTML ─────────────────────────────────────────────────────────────────
log("\nPatching HTML…")
html = replace_json(html, "D", D)
DIAG.write_text(html, encoding="utf-8")
ok(f"Saved → {DIAG.name}  ({len(html)//1024} KB)")

if push:
    log("\nPushing to GitHub…")
    try:
        subprocess.run(
            ["git", "-C", str(BASE), "add", "GraspAdAccount_Performance_Diagnosis.html"],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(BASE), "commit", "-m", "Inject Amplitude data into diagnosis report"],
            check=True, capture_output=True
        )
        subprocess.run(["git", "-C", str(BASE), "push"], check=True, capture_output=True)
        ok("Pushed")
    except subprocess.CalledProcessError as e:
        err(f"Git: {e.stderr.decode().strip() if e.stderr else e}")
else:
    log("\n[--no-push] Skipping git push.")

log("\n" + "=" * 60)
log("Done!")
log("=" * 60)
