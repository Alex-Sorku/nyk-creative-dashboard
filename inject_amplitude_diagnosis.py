#!/usr/bin/env python3
"""
inject_amplitude_diagnosis.py
──────────────────────────────────────────────────────────────────
Fetches Amplitude purchase_completed_success uniques (Grasp only)
and injects amp_pur / CPA_amp_pur / CVR_ra / CVR_ca into the
RECENT periods of GraspAdAccount_Performance_Diagnosis.html.

Recent periods = Jun (running), last_3d, yesterday.
Historical months (Jan–May) keep their original app-based CVRs
with no amp_pur column filled in.
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
    """Return (date_from, date_to) ISO strings, or (None, None) if unknown."""
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

def is_historical_month(pk, yesterday):
    """True for complete past months like 'Jan 26', 'Feb 26', etc."""
    for mo, mn in MONTH_MAP.items():
        if pk.startswith(mo + ' '):
            yr_raw = pk[len(mo)+1:len(mo)+3]
            try:
                yr = int('20' + yr_raw)
            except ValueError:
                continue
            if yr < yesterday.year or (yr == yesterday.year and mn < yesterday.month):
                return True
    return False

def fetch_amp_grasp(date_from, date_to, grasp_ids):
    """
    Fetch total unique purchases for Grasp campaigns only.
    Uses group-by utm_content and sums only Grasp ad IDs.
    """
    creds = base64.b64encode(f"{AMP_API_KEY}:{AMP_SECRET}".encode()).decode()
    hdrs  = {"Authorization": f"Basic {creds}"}
    params = {
        "e":     json.dumps({"event_type": "purchase_completed_success"}),
        "m":     "uniques",
        "g":     "gp:utm_content",
        "start": date_from.replace("-", ""),
        "end":   date_to.replace("-", ""),
        "i":     1,
        "limit": 300,
    }
    url = AMP_SEG_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except Exception as e:
        err(f"Amplitude API error for {date_from}→{date_to}: {e}")
        return 0

    total = 0
    matched = 0
    if resp and "data" in resp:
        labels = resp["data"].get("seriesLabels", [])
        series = resp["data"].get("series", [])
        for label, vals in zip(labels, series):
            if str(label) in grasp_ids:
                total += int(sum(vals))
                matched += 1
    info(f"    {date_from}→{date_to}: {matched} Grasp ad IDs matched, amp_pur={total}")
    return total

def extract_json(html, var):
    m = re.search(r'const\s+' + var + r'\s*=\s*(\{.*?\}|\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError(f"Cannot find {var}")
    return json.loads(m.group(1))

def replace_json(html, var, data):
    j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    new_html, n = re.subn(
        r'(const\s+' + var + r'\s*=\s*)(\{.*?\}|\[.*?\]);',
        lambda m: m.group(1) + j + ';',
        html, count=1, flags=re.DOTALL,
    )
    if n == 0:
        raise ValueError(f"Cannot replace {var}")
    return new_html

# ── Load files ─────────────────────────────────────────────────────────────────
log("=" * 60)
log("Amplitude injection → GraspAdAccount_Performance_Diagnosis.html")
log("=" * 60)

html      = DIAG.read_text(encoding="utf-8")
dash_html = DASH.read_text(encoding="utf-8")
D = extract_json(html, "D")
ok(f"Loaded D with periods: {D['period_order']}")

# Get Grasp ad IDs from creative_dashboard RAW_ADS
raw_ads = extract_json(dash_html, "RAW_ADS")
grasp_ids = {a['id'] for a in raw_ads if a.get('agency', '').lower() == 'grasp'}
ok(f"Grasp ad IDs: {len(grasp_ids)}")

# Get RAW_DAILY for yesterday meta data
raw_daily = extract_json(dash_html, "RAW_DAILY")

yesterday = dt.date.today() - dt.timedelta(days=1)
info(f"Today: {dt.date.today()}, Yesterday: {yesterday}")

# ── Process each period ────────────────────────────────────────────────────────
log("\nProcessing periods…")
for pk in list(D['period_order']) + (['yesterday'] if 'yesterday' not in D['period_order'] else []):
    m_data = D['period_totals'].get(pk)
    if m_data is None:
        D['period_totals'][pk] = {}
        m_data = D['period_totals'][pk]

    if is_historical_month(pk, yesterday):
        # Revert to app-based CVR, clear amp fields
        apps = m_data.get('apps') or 0
        regs = m_data.get('regs') or 0
        lc   = m_data.get('link_clicks') or 0
        m_data['amp_pur']     = None
        m_data['CPA_amp_pur'] = None
        m_data['CVR_ra'] = round(apps / regs * 100, 4) if regs else None
        m_data['CVR_ca'] = round(apps / lc  * 100, 4) if lc   else None
        info(f"  {pk}: historical — amp_pur cleared, CVR restored (app-based)")
        continue

    # Recent period — fetch Grasp-filtered Amplitude
    d_from, d_to = period_to_dates(pk, yesterday)
    if d_from is None:
        info(f"  {pk}: cannot parse date range, skipping")
        continue

    log(f"  Fetching Amplitude for {pk!r} ({d_from} → {d_to})…")
    amp = fetch_amp_grasp(d_from, d_to, grasp_ids)

    sp = m_data.get('spend') or 0
    rg = m_data.get('regs')  or 0
    lc = m_data.get('link_clicks') or 0
    m_data['amp_pur']     = amp
    m_data['CPA_amp_pur'] = round(sp / amp, 2) if amp else None
    m_data['CVR_ra'] = round(amp / rg * 100, 4) if rg  else None
    m_data['CVR_ca'] = round(amp / lc * 100, 4) if lc  else None

# ── Build yesterday period from Grasp RAW_DAILY ────────────────────────────────
log("\nBuilding yesterday period from Grasp RAW_DAILY…")
yk   = 'yesterday'
ystr = yesterday.isoformat()

y_grasp = [r for r in raw_daily if r.get('day') == ystr and r['ad_id'] in grasp_ids]
sp=0; imp=0; clk=0; pur=0; reg=0
for r in y_grasp:
    sp  += r.get('spend') or 0
    imp += r.get('imp')   or 0
    clk += r.get('clk')   or 0
    pur += r.get('pur')   or 0
    reg += r.get('reg')   or 0
ok(f"Grasp yesterday: {len(y_grasp)} rows — spend=${sp:.2f} imp={imp} clk={clk} pur={pur} reg={reg}")

amp = D['period_totals'].get(yk, {}).get('amp_pur', 0) or 0
if y_grasp:
    D['period_totals'][yk] = {
        "spend":       round(sp, 2),
        "impressions": imp,
        "reach":       imp,
        "link_clicks": clk,
        "apps":        pur,
        "regs":        reg,
        "amp_pur":     amp,
        "CPM":         round(sp / imp * 1000, 2) if imp  else None,
        "CPP":         round(sp / pur,        2) if pur  else None,
        "CTR":         round(clk / imp * 100,  4) if imp  else None,
        "CPC":         round(sp / clk,         2) if clk  else None,
        "CPA_app":     round(sp / pur,         2) if pur  else None,
        "CPA_reg":     round(sp / reg,         2) if reg  else None,
        "CPA_amp_pur": round(sp / amp,         2) if amp  else None,
        "CVR_cr":      round(reg / clk * 100,  4) if clk  else None,
        "CVR_ra":      round(amp / reg * 100,  4) if reg  else None,
        "CVR_ca":      round(amp / clk * 100,  4) if clk  else None,
    }
    ok(f"Yesterday rebuilt from creative RAW_DAILY")
else:
    info(f"No RAW_DAILY data for {ystr} — keeping Meta CSV data for yesterday period")

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
ok(f"Saved → {DIAG.name}  ({len(html) // 1024} KB)")

# Print summary
log("\nSummary:")
for pk in D['period_order']:
    v = D['period_totals'].get(pk, {})
    log(f"  {pk}: spend=${v.get('spend',0):.2f}  amp_pur={v.get('amp_pur')}  CVR_ra={v.get('CVR_ra')}  CVR_ca={v.get('CVR_ca')}")

if push:
    log("\nPushing to GitHub…")
    try:
        subprocess.run(
            ["git", "-C", str(BASE), "add",
             "GraspAdAccount_Performance_Diagnosis.html",
             "inject_amplitude_diagnosis.py"],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(BASE), "commit",
             "-m", "Fix: Grasp-only Amplitude + yesterday, revert Jan–May CVR"],
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
