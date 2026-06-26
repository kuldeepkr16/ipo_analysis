#!/usr/bin/env python3
"""
IPO Tracker — pulls live Mainboard + SME IPO data from Chittorgarh, InvestorGain
and Groww, merges it, and prints two filtered summary tables (Mainboard / SME).

Data sources (reverse-engineered JSON/HTML endpoints, no API keys needed):
  - InvestorGain report API: name, category, status, dates, price, lot,
    issue size, P/E, GMP (+ recent low/high range), rating, overall subscription.
  - Chittorgarh subscription report API: QIB / sNII / bNII / NII / Retail /
    Employee subscription multiples + application counts.
  - Groww IPO page (data embedded in __NEXT_DATA__): isSme flag, price band
    low/high, lot size, overall subscription — used to cross-check/fill gaps.

Only real-time/structural data is covered here (dates, price, lot size, min
investment, issue size, subscription, GMP). Deep fundamentals (revenue, PAT,
valuation vs peers, business risk) are NOT scraped — that needs qualitative
research, not a deterministic API.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table

IG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://www.investorgain.com/",
}
CG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://www.chittorgarh.com/",
}
GROWW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
}

TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class IPO:
    name: str
    category: str  # "Mainboard" or "SME"
    status: str  # Open / Upcoming / Closed
    open_date: Optional[date] = None
    close_date: Optional[date] = None
    boa_date: Optional[date] = None
    listing_date: Optional[date] = None
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    lot_size: Optional[int] = None
    issue_size_cr: Optional[float] = None
    pe: Optional[float] = None
    gmp: Optional[float] = None
    gmp_pct: Optional[float] = None
    gmp_low: Optional[float] = None
    gmp_high: Optional[float] = None
    rating: Optional[int] = None
    anchor: bool = False
    overall_sub: Optional[float] = None
    qib_sub: Optional[float] = None
    nii_sub: Optional[float] = None
    retail_sub: Optional[float] = None
    emp_sub: Optional[float] = None
    applications: Optional[str] = None
    sources: set = field(default_factory=set)

    @property
    def min_investment(self) -> Optional[float]:
        if self.price_high is None or self.lot_size is None:
            return None
        return round(self.price_high * self.lot_size, 2)

    @property
    def day_label(self) -> str:
        today = date.today()
        if not self.open_date or not self.close_date:
            return "-"
        total_days = business_days_between(self.open_date, self.close_date)
        if self.status == "Open" and self.open_date <= today <= self.close_date:
            day_n = business_days_between(self.open_date, min(today, self.close_date))
            return f"Day {day_n}/{total_days}"
        if self.status == "Upcoming":
            return f"Opens {self.open_date.strftime('%d-%b')}"
        return "Closed"

    @property
    def subscription_strength(self) -> str:
        if self.overall_sub is None:
            return "-"
        if self.overall_sub < 1:
            return "Weak"
        if self.overall_sub < 3:
            return "Average"
        if self.overall_sub < 10:
            return "Strong"
        return "Exceptional"

    @property
    def gmp_trend(self) -> str:
        if self.gmp is None or self.gmp_low is None or self.gmp_high is None:
            return "-"
        if self.gmp_high == self.gmp_low:
            return "Flat"
        position = (self.gmp - self.gmp_low) / (self.gmp_high - self.gmp_low)
        if position >= 0.66:
            return "Rising"
        if position <= 0.33:
            return "Falling"
        return "Mixed"

    @property
    def quick_signal(self) -> str:
        """Heuristic only, from live GMP% + subscription strength. This is NOT
        a fundamentals-based Apply/Skip call (no revenue/valuation/peer data
        is available from these APIs) - just a directional read on demand."""
        if self.overall_sub is None:
            return "-"
        score = {"Weak": 0, "Average": 1, "Strong": 2, "Exceptional": 3}.get(self.subscription_strength, 0)
        if self.gmp_pct is not None:
            if self.gmp_pct < 0:
                score -= 2
            elif self.gmp_pct >= 20:
                score += 2
            elif self.gmp_pct >= 5:
                score += 1
        if score >= 3:
            return "Promising"
        if score <= 0:
            return "Caution"
        return "Neutral"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def normalize_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\b(ltd|limited|ipo|the)\b", "", name)
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name


def parse_money_cr(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([\d,]+\.?\d*)", text.replace("&#8377;", "").replace("₹", ""))
    return float(m.group(1).replace(",", "")) if m else None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_iso_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def business_days_between(start: date, end: date) -> int:
    """Count Mon-Fri days between start and end inclusive (NSE/BSE don't take
    IPO bids on weekends, so calendar-day counts overstate the bidding window)."""
    if end < start:
        return 0
    total_days = (end - start).days + 1
    return sum(1 for offset in range(total_days) if (start.weekday() + offset) % 7 < 5)


def compute_status(open_date: Optional[date], close_date: Optional[date]) -> str:
    """Date-range based status so an IPO stays 'Open' through its whole closing
    date, even after intraday bidding has ended (sources like InvestorGain flip
    their own badge to 'Closed' once the ~5pm cutoff passes)."""
    today = date.today()
    if open_date and today < open_date:
        return "Upcoming"
    if close_date and today > close_date:
        return "Closed"
    if open_date and close_date and open_date <= today <= close_date:
        return "Open"
    return "Closed"


# --------------------------------------------------------------------------- #
# Source 1: InvestorGain combined report (Mainboard + SME, one call)
# --------------------------------------------------------------------------- #

def fetch_investorgain(year: int) -> list[IPO]:
    fy = f"{year}-{str(year + 1)[-2:]}"
    url = (
        f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/"
        f"331/1/6/{year}/{fy}/0/all"
    )
    resp = requests.get(url, headers=IG_HEADERS, params={"search": ""}, timeout=TIMEOUT)
    resp.raise_for_status()
    rows = resp.json().get("reportTableData", [])

    ipos = []
    for row in rows:
        name = row.get("~ipo_name", "").strip()
        if not name:
            continue
        category = "Mainboard" if row.get("~IPO_Category") == "IPO" else "SME"

        gmp_text = row.get("GMP", "")
        gmp_match = re.search(r"<b>(-?[\d.]+|--)</b>\s*\(([-\d.]+)%\)", gmp_text)
        gmp = None if not gmp_match or gmp_match.group(1) == "--" else float(gmp_match.group(1))
        gmp_pct = float(gmp_match.group(2)) if gmp_match and gmp is not None else None
        range_match = re.search(
            r"<b>(-?[\d.]+)\s*(?:\\u2193|↓)\s*/\s*(-?[\d.]+)\s*(?:\\u2191|↑)</b>",
            gmp_text,
        )
        gmp_low = float(range_match.group(1)) if range_match else gmp
        gmp_high = float(range_match.group(2)) if range_match else gmp

        sub_text = row.get("Sub", "-")
        overall_sub = None
        if sub_text and sub_text != "-":
            m = re.search(r"([\d.]+)x", sub_text)
            if m:
                overall_sub = float(m.group(1))

        price = row.get("Price (₹)") or row.get("Price (₹)")
        price = float(price) if price else None

        lot = row.get("Lot")
        lot = int(lot) if lot else None

        rating_html = row.get("Rating", "")
        rating = rating_html.count("&#128293;") or None

        open_date = parse_iso_date(row.get("~Srt_Open"))
        close_date = parse_iso_date(row.get("~Srt_Close"))
        status = compute_status(open_date, close_date)

        ipo = IPO(
            name=name,
            category=category,
            status=status,
            open_date=open_date,
            close_date=close_date,
            boa_date=parse_iso_date(row.get("~Srt_BoA_Dt")),
            listing_date=parse_iso_date(row.get("~Str_Listing")),
            price_low=price,
            price_high=price,
            lot_size=lot,
            issue_size_cr=parse_money_cr(row.get("IPO Size", "")),
            pe=float(row["~P/E"]) if row.get("~P/E") not in (None, "--", "") else None,
            gmp=gmp,
            gmp_pct=gmp_pct,
            gmp_low=gmp_low,
            gmp_high=gmp_high,
            rating=rating,
            anchor="check" in row.get("Anchor", "").lower() or "✅" in row.get("Anchor", ""),
            overall_sub=overall_sub,
        )
        ipo.sources.add("investorgain")
        ipos.append(ipo)
    return ipos


# --------------------------------------------------------------------------- #
# Source 2: Chittorgarh subscription breakdown (mainboard + sme, two calls)
# --------------------------------------------------------------------------- #

def fetch_chittorgarh_subscription(year: int, segment: str) -> dict[str, dict]:
    fy = f"{year}-{str(year + 1)[-2:]}"
    url = (
        f"https://webnodejs.chittorgarh.com/cloud/report/data-read/"
        f"21/1/6/{year}/{fy}/0/{segment}/0"
    )
    try:
        resp = requests.get(url, headers=CG_HEADERS, params={"search": ""}, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("reportTableData", [])
    except (requests.RequestException, ValueError):
        return {}

    result = {}
    for row in rows:
        company_html = row.get("Company", "")
        name = strip_html(re.sub(r'<span[^>]*>.*?</span>', '', company_html))
        name = re.sub(r"\s+(Ltd\.?|Limited)$", "", name).strip()
        key = normalize_name(name)
        result[key] = {
            "qib_sub": row.get("QIB (x)") or None,
            "nii_sub": row.get("NII (x)") or None,
            "retail_sub": row.get("Retail (x)") or None,
            "emp_sub": row.get("Employee (x)") or None,
            "applications": row.get("Applications") or None,
        }
    return result


# --------------------------------------------------------------------------- #
# Source 3: Groww IPO page (__NEXT_DATA__ embedded JSON)
# --------------------------------------------------------------------------- #

def fetch_groww() -> dict[str, dict]:
    try:
        resp = requests.get("https://groww.in/ipo", headers=GROWW_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException:
        return {}

    idx = html.find("__NEXT_DATA__")
    if idx < 0:
        return {}
    start = html.find(">", idx) + 1
    end = html.find("</script>", start)
    try:
        import json
        data = json.loads(html[start:end])
    except ValueError:
        return {}

    page_props = data.get("props", {}).get("pageProps", {})
    all_records = (
        page_props.get("openDataList", [])
        + page_props.get("closedDataList", [])
        + page_props.get("upcomingDataList", [])
    )

    result = {}
    for r in all_records:
        company = r.get("companyName")
        if not company:
            continue
        key = normalize_name(company)
        categories = r.get("categories", [])
        price_low = min((c["minPrice"] for c in categories if c.get("minPrice")), default=None)
        price_high = max((c["maxPrice"] for c in categories if c.get("maxPrice")), default=r.get("issuePrice"))
        lot_size = next((c["lotSize"] for c in categories if c.get("lotSize")), None)
        result[key] = {
            "is_sme": r.get("isSme", False),
            "price_low": price_low,
            "price_high": price_high,
            "lot_size": lot_size,
            "overall_sub": r.get("overallSubscription"),
        }
    return result


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def build_ipo_list(year: int) -> list[IPO]:
    ipos = fetch_investorgain(year)

    cg_mainboard = fetch_chittorgarh_subscription(year, "mainboard")
    cg_sme = fetch_chittorgarh_subscription(year, "sme")
    cg_combined = {**cg_mainboard, **cg_sme}

    groww = fetch_groww()

    for ipo in ipos:
        key = normalize_name(ipo.name)

        cg = cg_combined.get(key)
        if cg:
            def _f(v: object) -> Optional[float]:
                try:
                    return float(v) if v is not None else None
                except (ValueError, TypeError):
                    return None

            ipo.qib_sub = _f(cg["qib_sub"])
            ipo.nii_sub = _f(cg["nii_sub"])
            ipo.retail_sub = _f(cg["retail_sub"])
            ipo.emp_sub = _f(cg["emp_sub"])
            ipo.applications = cg["applications"]
            ipo.sources.add("chittorgarh")

        gw = groww.get(key)
        if gw:
            if gw.get("price_low"):
                ipo.price_low = gw["price_low"]
            if gw.get("price_high"):
                ipo.price_high = gw["price_high"]
            if gw.get("lot_size"):
                ipo.lot_size = gw["lot_size"]
            if ipo.overall_sub is None and gw.get("overall_sub") is not None:
                ipo.overall_sub = gw["overall_sub"]
            ipo.sources.add("groww")

    return ipos


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

def apply_filters(ipos: list[IPO], args: argparse.Namespace) -> list[IPO]:
    out = []
    for ipo in ipos:
        if args.status != "all" and ipo.status.lower() != args.status:
            continue
        if args.min_gmp_pct is not None and (ipo.gmp_pct is None or ipo.gmp_pct < args.min_gmp_pct):
            continue
        if args.max_investment is not None and (
            ipo.min_investment is None or ipo.min_investment > args.max_investment
        ):
            continue
        if args.min_subscription is not None and (
            ipo.overall_sub is None or ipo.overall_sub < args.min_subscription
        ):
            continue
        if args.min_rating is not None and (ipo.rating is None or ipo.rating < args.min_rating):
            continue
        out.append(ipo)
    return out


def sort_ipos(ipos: list[IPO], sort_by: str) -> list[IPO]:
    keymap = {
        "gmp": lambda i: i.gmp_pct if i.gmp_pct is not None else -999,
        "subscription": lambda i: i.overall_sub if i.overall_sub is not None else -1,
        "close_date": lambda i: i.close_date or date.max,
        "rating": lambda i: i.rating or 0,
        "name": lambda i: i.name,
    }
    return sorted(ipos, key=keymap.get(sort_by, keymap["gmp"]), reverse=(sort_by != "close_date" and sort_by != "name"))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d-%b") if d else "-"


def fmt_money(v: Optional[float]) -> str:
    return f"₹{v:,.0f}" if v is not None else "-"


def render_table(ipos: list[IPO], title: str, console: Console) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Company")
    table.add_column("Category")
    table.add_column("Status")
    table.add_column("Day")
    table.add_column("Open-Close")
    table.add_column("Listing")
    table.add_column("Price Band")
    table.add_column("Lot")
    table.add_column("Min Invest")
    table.add_column("Issue Size")
    table.add_column("Sub (Ovr)")
    table.add_column("QIB")
    table.add_column("NII")
    table.add_column("Retail")
    table.add_column("Sub Strength")
    table.add_column("GMP")
    table.add_column("GMP %")
    table.add_column("GMP Trend")
    table.add_column("Rating")
    table.add_column("Quick Signal")

    if not ipos:
        console.print(f"[dim]{title}: no IPOs match the current filters.[/dim]")
        return

    for ipo in ipos:
        price_band = (
            f"{ipo.price_low:.0f}-{ipo.price_high:.0f}"
            if ipo.price_low and ipo.price_high and ipo.price_low != ipo.price_high
            else (f"{ipo.price_high:.0f}" if ipo.price_high else "-")
        )
        table.add_row(
            ipo.name,
            ipo.category,
            ipo.status,
            ipo.day_label,
            f"{fmt_date(ipo.open_date)} → {fmt_date(ipo.close_date)}",
            fmt_date(ipo.listing_date),
            price_band,
            str(ipo.lot_size) if ipo.lot_size else "-",
            fmt_money(ipo.min_investment),
            f"₹{ipo.issue_size_cr:.1f}Cr" if ipo.issue_size_cr else "-",
            f"{ipo.overall_sub:.2f}x" if ipo.overall_sub is not None else "-",
            f"{ipo.qib_sub:.2f}x" if ipo.qib_sub is not None else "-",
            f"{ipo.nii_sub:.2f}x" if ipo.nii_sub is not None else "-",
            f"{ipo.retail_sub:.2f}x" if ipo.retail_sub is not None else "-",
            ipo.subscription_strength,
            f"₹{ipo.gmp:.0f}" if ipo.gmp is not None else "-",
            f"{ipo.gmp_pct:.1f}%" if ipo.gmp_pct is not None else "-",
            ipo.gmp_trend,
            f"{ipo.rating}/5" if ipo.rating else "-",
            ipo.quick_signal,
        )
    console.print(table)
    console.print(
        "[dim]Quick Signal is a heuristic from live GMP% + subscription only - "
        "it is NOT a fundamentals-based Apply/Skip call (no revenue/valuation/peer "
        "data is scraped here).[/dim]"
    )


def save_csv(ipos: list[IPO], path: str) -> None:
    import csv

    fields = [
        "name", "category", "status", "day_label", "open_date", "close_date",
        "listing_date", "price_low", "price_high", "lot_size", "min_investment",
        "issue_size_cr", "overall_sub", "qib_sub", "nii_sub", "retail_sub",
        "subscription_strength", "gmp", "gmp_pct", "gmp_trend", "rating",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for ipo in ipos:
            writer.writerow([
                ipo.name, ipo.category, ipo.status, ipo.day_label, ipo.open_date,
                ipo.close_date, ipo.listing_date, ipo.price_low, ipo.price_high,
                ipo.lot_size, ipo.min_investment, ipo.issue_size_cr, ipo.overall_sub,
                ipo.qib_sub, ipo.nii_sub, ipo.retail_sub, ipo.subscription_strength,
                ipo.gmp, ipo.gmp_pct, ipo.gmp_trend, ipo.rating,
            ])


# --------------------------------------------------------------------------- #
# HTML output (for GitHub Pages / static deployment)
# --------------------------------------------------------------------------- #

def ipo_to_dict(ipo: IPO) -> dict:
    return {
        "name": ipo.name,
        "category": ipo.category,
        "status": ipo.status,
        "openDate":    ipo.open_date.isoformat()    if ipo.open_date    else None,
        "closeDate":   ipo.close_date.isoformat()   if ipo.close_date   else None,
        "boaDate":     ipo.boa_date.isoformat()     if ipo.boa_date     else None,
        "listingDate": ipo.listing_date.isoformat() if ipo.listing_date else None,
        "priceLow":    ipo.price_low,
        "priceHigh":   ipo.price_high,
        "lotSize":     ipo.lot_size,
        "issueSizeCr": ipo.issue_size_cr,
        "pe":          ipo.pe,
        "gmp":         ipo.gmp,
        "gmpPct":      ipo.gmp_pct,
        "gmpLow":      ipo.gmp_low,
        "gmpHigh":     ipo.gmp_high,
        "rating":      ipo.rating,
        "anchor":      ipo.anchor,
        "overallSub":  ipo.overall_sub,
        "qibSub":      ipo.qib_sub,
        "niiSub":      ipo.nii_sub,
        "retailSub":   ipo.retail_sub,
        "empSub":      ipo.emp_sub,
        "applications": ipo.applications,
        "sources":     sorted(ipo.sources),
    }


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPO Tracker — India</title>
<meta name="description" content="Mainboard and SME IPO dashboard for Indian markets: GMP, subscription breakdown, quick signals.">
<script>window.IPO_SNAPSHOT={generatedAt:"__GENERATED_AT__",data:__IPO_DATA__};</script>
<style>
:root{--bg:#EDF2FA;--surface:#FFFFFF;--surface-2:#E4EDF8;--border:#CDD7E8;--border-2:#B8C6DB;--accent:#1D4ED8;--violet:#6D28D9;--green:#15803D;--amber:#B45309;--red:#B91C1C;--text:#0F1E3C;--text-2:#4A5E7A;--text-3:#94A3B8;--mono:'SF Mono','Cascadia Code','Fira Code',Consolas,monospace;--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;line-height:1.5;min-height:100vh}
.hdr{position:sticky;top:0;z-index:100;background:rgba(237,242,250,.95);backdrop-filter:blur(14px);border-bottom:1px solid var(--border);padding:13px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.hdr-left{display:flex;align-items:baseline;gap:10px}
.hdr-title{font-size:17px;font-weight:700;letter-spacing:-.025em}
.hdr-sub{font-size:11px;color:var(--text-3);font-family:var(--mono)}
.hdr-right{display:flex;align-items:center;gap:12px}
.updated{font-size:11px;font-family:var(--mono);color:var(--text-3);display:flex;align-items:center;gap:6px}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.gh-badge{font-size:10.5px;padding:4px 10px;border-radius:5px;background:var(--surface-2);border:1px solid var(--border-2);color:var(--text-3);text-decoration:none;display:flex;align-items:center;gap:5px;transition:color .12s}
.gh-badge:hover{color:var(--text)}
.controls{padding:12px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--border);background:var(--surface)}
.ctrl-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-3)}
.tab-group{display:flex;background:var(--bg);border:1px solid var(--border);border-radius:5px;overflow:hidden}
.tab{background:none;border:none;border-right:1px solid var(--border);color:var(--text-2);font-size:11.5px;padding:4px 12px;cursor:pointer;font-family:var(--sans);transition:background .1s,color .1s}
.tab:last-child{border-right:none}
.tab.active{background:var(--accent);color:#fff}
.tab:hover:not(.active){background:var(--surface-2);color:var(--text)}
.sel{background:var(--bg);border:1px solid var(--border);color:var(--text-2);font-size:11.5px;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:var(--sans);outline:none}
.sel:focus{border-color:var(--accent);color:var(--text)}
#ctrl-stats{margin-left:auto;font-size:11px;color:var(--text-3);font-family:var(--mono)}
.main{padding:24px;display:flex;flex-direction:column;gap:28px}
.sec-hdr{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.sec-stripe{display:inline-block;width:3px;height:16px;border-radius:2px}
.sec-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-2)}
.sec-count{font-size:10px;font-family:var(--mono);color:var(--text-3);background:var(--surface-2);border:1px solid var(--border);padding:1px 6px;border-radius:8px}
.tbl-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:7px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:12px;min-width:1480px}
thead{background:var(--surface-2)}
th{padding:9px 11px;text-align:left;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);white-space:nowrap;border-bottom:1px solid var(--border);user-select:none}
td{padding:8px 11px;border-bottom:1px solid var(--border);color:var(--text-2);white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(29,78,216,.04)}
.td-name{font-family:var(--sans);font-size:12.5px;font-weight:500;color:var(--text);max-width:180px;overflow:hidden;text-overflow:ellipsis}
.pill{display:inline-flex;align-items:center;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;font-family:var(--sans)}
.p-open{background:rgba(21,128,61,.1);color:var(--green)}
.p-upcoming{background:rgba(180,83,9,.1);color:var(--amber)}
.p-closed{background:rgba(148,163,184,.2);color:var(--text-3)}
.pos{color:var(--green)}.neg{color:var(--red)}.dim{color:var(--text-3)}.amb{color:var(--amber)}
.sig{font-family:var(--sans);font-size:11px;font-weight:700}
.sig-promise{color:var(--green)}.sig-neutral{color:var(--amber)}.sig-caution{color:var(--red)}
.str-exc{color:var(--green);font-weight:700}.str-str{color:#86EFAC}.str-avg{color:var(--amber)}.str-wk{color:var(--red)}
.empty-row td{text-align:center;padding:28px;color:var(--text-3);font-family:var(--sans);font-style:italic;font-size:12px}
.disclaimer{margin:0 24px 28px;padding:11px 16px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:11px;color:var(--text-3);line-height:1.65}
.src-badges{display:flex;gap:3px}
.src{font-size:9px;padding:1px 5px;border-radius:3px;background:var(--surface-2);color:var(--text-3);font-family:var(--sans);text-transform:uppercase;letter-spacing:.04em}
.cards{display:none}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:10px}
.card-hdr{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.card-name{font-size:14px;font-weight:600;color:var(--text);line-height:1.3;flex:1}
.card-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 16px}
.metric{display:flex;flex-direction:column;gap:3px}
.metric-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3)}
.metric-val{font-family:var(--mono);font-size:13px;color:var(--text-2);font-variant-numeric:tabular-nums}
.card-footer{display:flex;align-items:center;justify-content:space-between;padding-top:10px;border-top:1px solid var(--border)}
.card-day{font-size:11px;color:var(--text-3);font-family:var(--mono)}
@media(max-width:768px){
  .hdr{padding:11px 16px}
  .controls{padding:10px 16px;gap:10px}
  .main{padding:12px;gap:20px}
  .hdr-sub{display:none}
  .tbl-wrap{display:none}
  .cards{display:flex;flex-direction:column;gap:10px}
  .disclaimer{margin:0 12px 20px;font-size:11px}
}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-left">
    <span class="hdr-title">IPO Tracker</span>
    <span class="hdr-sub">India · Mainboard &amp; SME</span>
  </div>
  <div class="hdr-right">
    <span class="updated" id="updated-label">—</span>
    <a class="gh-badge" href="https://github.com" target="_blank" rel="noopener">
      <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
      Auto-updated via GitHub Actions
    </a>
  </div>
</div>
<div class="controls">
  <div style="display:flex;align-items:center;gap:8px">
    <span class="ctrl-label">Status</span>
    <div class="tab-group" id="status-tabs">
      <button class="tab active" data-s="open"     onclick="setStatus('open')">Open</button>
      <button class="tab"        data-s="upcoming" onclick="setStatus('upcoming')">Upcoming</button>
      <button class="tab"        data-s="closed"   onclick="setStatus('closed')">Closed</button>
      <button class="tab"        data-s="all"      onclick="setStatus('all')">All</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <span class="ctrl-label">Sort</span>
    <select class="sel" onchange="setSort(this.value)">
      <option value="gmp">GMP %</option>
      <option value="subscription">Subscription</option>
      <option value="close_date">Close Date</option>
      <option value="rating">Rating</option>
      <option value="name">Name</option>
    </select>
  </div>
  <span id="ctrl-stats" style="margin-left:auto"></span>
</div>
<div id="content"></div>
<script>
var allIpos=[],statusFilter='open',sortBy='gmp';
var snap=window.IPO_SNAPSHOT;

function parseD(s){if(!s)return null;var d=new Date(s+'T00:00:00');return isNaN(d.getTime())?null:d;}
function today(){var d=new Date();d.setHours(0,0,0,0);return d;}
function fmtDate(d){return d?d.toLocaleDateString('en-IN',{day:'2-digit',month:'short'}):'-';}
function toFloat(v){if(v===null||v===undefined||v==='')return null;var n=parseFloat(v);return isNaN(n)?null:n;}
function bdays(s,e){if(!s||!e||e<s)return 0;var n=0,c=new Date(s);while(c<=e){var d=c.getDay();if(d&&d<6)n++;c.setDate(c.getDate()+1);}return n;}
function normName(n){return n.toLowerCase().replace(/\\b(ltd|limited|ipo|the)\\b/g,'').replace(/[^a-z0-9]+/g,'');}

function loadIpos(){
  return snap.data.map(function(d){
    return Object.assign({},d,{
      openDate:parseD(d.openDate),closeDate:parseD(d.closeDate),
      boaDate:parseD(d.boaDate),listingDate:parseD(d.listingDate),
      sources:new Set(d.sources||[])
    });
  });
}

function minInv(ipo){return(ipo.priceHigh!==null&&ipo.lotSize!==null)?Math.round(ipo.priceHigh*ipo.lotSize):null;}
function dayLabel(ipo){
  var now=today();
  if(!ipo.openDate||!ipo.closeDate)return'-';
  var tot=bdays(ipo.openDate,ipo.closeDate);
  if(ipo.status==='Open'){var e=now<ipo.closeDate?now:ipo.closeDate;return'Day '+bdays(ipo.openDate,e)+'/'+tot;}
  if(ipo.status==='Upcoming')return'Opens '+fmtDate(ipo.openDate);
  return'Closed';
}
function subStr(ipo){
  if(ipo.overallSub===null)return'-';
  if(ipo.overallSub<1)return'Weak';
  if(ipo.overallSub<3)return'Average';
  if(ipo.overallSub<10)return'Strong';
  return'Exceptional';
}
function gmpTrend(ipo){
  if(ipo.gmp===null||ipo.gmpLow===null||ipo.gmpHigh===null)return'-';
  if(ipo.gmpHigh===ipo.gmpLow)return'Flat';
  var p=(ipo.gmp-ipo.gmpLow)/(ipo.gmpHigh-ipo.gmpLow);
  return p>=0.66?'Rising':p<=0.33?'Falling':'Mixed';
}
function quickSig(ipo){
  if(ipo.overallSub===null)return'-';
  var sc={Weak:0,Average:1,Strong:2,Exceptional:3}[subStr(ipo)]||0;
  if(ipo.gmpPct!==null){if(ipo.gmpPct<0)sc-=2;else if(ipo.gmpPct>=20)sc+=2;else if(ipo.gmpPct>=5)sc+=1;}
  return sc>=3?'Promising':sc<=0?'Caution':'Neutral';
}

var fmtMoney=function(v){return v!==null?'\\u20b9'+v.toLocaleString('en-IN'):'-';};
var fmtSub=function(v){return v!==null?v.toFixed(2)+'x':'-';};

function sPill(s){var c={Open:'p-open',Upcoming:'p-upcoming',Closed:'p-closed'}[s]||'p-closed';return'<span class="pill '+c+'">'+s+'</span>';}
function gmpCell(ipo){
  if(ipo.gmp===null)return'<span class="dim">-</span>';
  var cls=ipo.gmp>0?'pos':ipo.gmp<0?'neg':'';
  var pct=ipo.gmpPct!==null?' <span style="opacity:.6">('+ipo.gmpPct.toFixed(1)+'%)</span>':'';
  return'<span class="'+cls+'">₹'+ipo.gmp.toFixed(0)+pct+'</span>';
}
function strCell(ipo){var s=subStr(ipo);var c={Exceptional:'str-exc',Strong:'str-str',Average:'str-avg',Weak:'str-wk'}[s]||'dim';return'<span class="'+c+'">'+s+'</span>';}
function sigCell(ipo){var s=quickSig(ipo);if(s==='-')return'<span class="dim">-</span>';var c={Promising:'sig-promise',Neutral:'sig-neutral',Caution:'sig-caution'}[s];return'<span class="sig '+c+'">'+s+'</span>';}
function trendCell(ipo){var t=gmpTrend(ipo);if(t==='-')return'<span class="dim">-</span>';var arr={Rising:'\\u2191',Falling:'\\u2193',Flat:'\\u2192',Mixed:'\\u2248'}[t]||'';var c={Rising:'pos',Falling:'neg',Flat:'dim',Mixed:'amb'}[t]||'';return'<span class="'+c+'">'+arr+' '+t+'</span>';}
function priceBand(ipo){
  if(!ipo.priceLow&&!ipo.priceHigh)return'-';
  if(ipo.priceLow&&ipo.priceHigh&&ipo.priceLow!==ipo.priceHigh)return ipo.priceLow.toFixed(0)+'\\u2013'+ipo.priceHigh.toFixed(0);
  return(ipo.priceHigh||ipo.priceLow).toFixed(0);
}

function applyFilters(ipos){return statusFilter==='all'?ipos:ipos.filter(function(i){return i.status.toLowerCase()===statusFilter;});}
function sortIpos(ipos){
  var a=ipos.slice();
  if(sortBy==='gmp')a.sort(function(x,y){return(y.gmpPct!=null?y.gmpPct:-999)-(x.gmpPct!=null?x.gmpPct:-999);});
  else if(sortBy==='subscription')a.sort(function(x,y){return(y.overallSub!=null?y.overallSub:-1)-(x.overallSub!=null?x.overallSub:-1);});
  else if(sortBy==='close_date')a.sort(function(x,y){return(x.closeDate?x.closeDate.getTime():Infinity)-(y.closeDate?y.closeDate.getTime():Infinity);});
  else if(sortBy==='rating')a.sort(function(x,y){return(y.rating||0)-(x.rating||0);});
  else a.sort(function(x,y){return x.name.localeCompare(y.name);});
  return a;
}

function renderCards(ipos,category){
  var list=sortIpos(applyFilters(ipos.filter(function(i){return i.category===category;})));
  if(list.length===0)return'<div style="text-align:center;padding:24px;color:var(--text-3);font-style:italic;font-size:13px">No '+category+' IPOs match current filters.</div>';
  return list.map(function(ipo){
    var mi=minInv(ipo);
    return'<div class="card">'
      +'<div class="card-hdr"><span class="card-name">'+ipo.name+'</span>'+sPill(ipo.status)+'</div>'
      +'<div class="card-grid">'
      +'<div class="metric"><span class="metric-lbl">GMP</span><span class="metric-val">'+gmpCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Subscription</span><span class="metric-val">'+fmtSub(ipo.overallSub)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Min Invest</span><span class="metric-val">'+(mi!==null?fmtMoney(mi):'-')+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Closes</span><span class="metric-val">'+fmtDate(ipo.closeDate)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Price Band</span><span class="metric-val">'+priceBand(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Rating</span><span class="metric-val">'+(ipo.rating!==null?'<span style="color:var(--amber)">'+ipo.rating+'</span><span style="color:var(--text-3)">/5</span>':'<span class="dim">-</span>')+'</span></div>'
      +'</div>'
      +'<div class="card-footer"><span class="card-day">'+dayLabel(ipo)+'</span>'+sigCell(ipo)+'</div>'
      +'</div>';
  }).join('');
}

function renderSection(ipos,category){
  var list=sortIpos(applyFilters(ipos.filter(function(i){return i.category===category;})));
  var color=category==='Mainboard'?'var(--accent)':'var(--violet)';
  var rows=list.length===0
    ?'<tr class="empty-row"><td colspan="20">No '+category+' IPOs match current filters.</td></tr>'
    :list.map(function(ipo){
      var mi=minInv(ipo);
      var srcs=[...ipo.sources].map(function(s){return'<span class="src">'+s+'</span>';}).join('');
      return'<tr>'
        +'<td class="td-name" title="'+ipo.name+'">'+ipo.name+'</td>'
        +'<td>'+sPill(ipo.status)+'</td>'
        +'<td>'+dayLabel(ipo)+'</td>'
        +'<td>'+fmtDate(ipo.openDate)+' \\u2192 '+fmtDate(ipo.closeDate)+'</td>'
        +'<td>'+fmtDate(ipo.listingDate)+'</td>'
        +'<td>'+priceBand(ipo)+'</td>'
        +'<td>'+(ipo.lotSize!=null?ipo.lotSize:'-')+'</td>'
        +'<td>'+(mi!==null?fmtMoney(mi):'-')+'</td>'
        +'<td>'+(ipo.issueSizeCr!==null?'\\u20b9'+ipo.issueSizeCr.toFixed(1)+'Cr':'-')+'</td>'
        +'<td>'+fmtSub(ipo.overallSub)+'</td>'
        +'<td>'+fmtSub(ipo.qibSub)+'</td>'
        +'<td>'+fmtSub(ipo.niiSub)+'</td>'
        +'<td>'+fmtSub(ipo.retailSub)+'</td>'
        +'<td>'+strCell(ipo)+'</td>'
        +'<td>'+gmpCell(ipo)+'</td>'
        +'<td>'+trendCell(ipo)+'</td>'
        +'<td>'+(ipo.rating!==null?'<span style="color:var(--amber)">'+ipo.rating+'</span><span style="color:var(--text-3)">/5</span>':'<span class="dim">-</span>')+'</td>'
        +'<td>'+(ipo.pe!==null?ipo.pe.toFixed(1)+'x':'<span class="dim">-</span>')+'</td>'
        +'<td>'+sigCell(ipo)+'</td>'
        +'<td><div class="src-badges">'+srcs+'</div></td>'
        +'</tr>';
    }).join('');
  return'<div>'
    +'<div class="sec-hdr">'
    +'<span class="sec-stripe" style="background:'+color+'"></span>'
    +'<span class="sec-title">'+category.toUpperCase()+'</span>'
    +'<span class="sec-count">'+list.length+'</span>'
    +'</div>'
    +'<div class="cards">'+renderCards(ipos,category)+'</div>'
    +'<div class="tbl-wrap"><table>'
    +'<thead><tr>'
    +'<th>Company</th><th>Status</th><th>Day</th><th>Open \\u2192 Close</th>'
    +'<th>Listing</th><th>Price Band</th><th>Lot</th><th>Min Invest</th>'
    +'<th>Issue Size</th><th>Overall Sub</th><th>QIB</th><th>NII</th>'
    +'<th>Retail</th><th>Sub Strength</th><th>GMP</th><th>GMP Trend</th>'
    +'<th>Rating</th><th>P/E</th><th>Quick Signal \\u26a0</th><th>Sources</th>'
    +'</tr></thead>'
    +'<tbody>'+rows+'</tbody></table></div></div>';
}

function renderContent(){
  var filtered=applyFilters(allIpos);
  var mb=filtered.filter(function(i){return i.category==='Mainboard';}).length;
  var sme=filtered.filter(function(i){return i.category==='SME';}).length;
  document.getElementById('ctrl-stats').textContent=mb+' mainboard \\u00b7 '+sme+' SME';
  document.getElementById('content').innerHTML=
    '<div class="main">'+renderSection(allIpos,'Mainboard')+renderSection(allIpos,'SME')+'</div>'
    +'<div class="disclaimer">\\u26a0 <strong>Quick Signal</strong> is a heuristic from live GMP% + subscription only \\u2014 not a fundamentals-based Apply/Skip call. No revenue, valuation, or peer data included. Data sourced from InvestorGain and Chittorgarh. Updated automatically every 2 hours via GitHub Actions.</div>';
}

function setStatus(s){
  statusFilter=s;
  document.querySelectorAll('#status-tabs .tab').forEach(function(b){b.classList.toggle('active',b.dataset.s===s);});
  renderContent();
}
function setSort(s){sortBy=s;renderContent();}

(function init(){
  allIpos=loadIpos();
  var t=new Date(snap.generatedAt);
  document.getElementById('updated-label').innerHTML=
    '<span class="live-dot"></span> '+
    t.toLocaleDateString('en-IN',{day:'2-digit',month:'short'})+' '+
    t.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'})+' UTC';
  renderContent();
})();
</script>
</body>
</html>"""


def save_html(ipos: list[IPO], path: str) -> None:
    import json
    import os
    from datetime import datetime, timezone

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_json = json.dumps([ipo_to_dict(i) for i in ipos], ensure_ascii=False)
    html = _HTML_TEMPLATE.replace("__GENERATED_AT__", generated_at).replace("__IPO_DATA__", data_json)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live Mainboard + SME IPO tracker (India)")
    p.add_argument("--status", choices=["open", "upcoming", "closed", "all"], default="open",
                    help="Filter by IPO lifecycle status (default: open)")
    p.add_argument("--min-gmp-pct", type=float, default=None, help="Only show IPOs with GMP%% >= this value")
    p.add_argument("--max-investment", type=float, default=None, help="Only show IPOs with min investment <= this value (Rs)")
    p.add_argument("--min-subscription", type=float, default=None, help="Only show IPOs with overall subscription >= this value")
    p.add_argument("--min-rating", type=int, default=None, help="Only show IPOs with InvestorGain rating >= this value (1-5)")
    p.add_argument("--sort-by", choices=["gmp", "subscription", "close_date", "rating", "name"], default="gmp")
    p.add_argument("--year", type=int, default=date.today().year, help="IPO calendar year to query")
    p.add_argument("--save-csv", metavar="DIR", default=None, help="Also save Mainboard/SME tables as CSV files in this directory")
    p.add_argument("--output-html", metavar="PATH", default=None,
                   help="Write a self-contained HTML dashboard to this path (for GitHub Pages)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    console = Console()

    console.print("[bold]Fetching live IPO data from Chittorgarh, InvestorGain and Groww...[/bold]")
    ipos = build_ipo_list(args.year)
    if not ipos:
        console.print("[red]Could not fetch IPO data from InvestorGain — check connectivity / site structure.[/red]")
        sys.exit(1)

    filtered = apply_filters(ipos, args)

    mainboard = sort_ipos([i for i in filtered if i.category == "Mainboard"], args.sort_by)
    sme = sort_ipos([i for i in filtered if i.category == "SME"], args.sort_by)
    combined = mainboard + sme

    render_table(mainboard, f"Mainboard IPOs — status={args.status}", console)
    render_table(sme, f"SME IPOs — status={args.status}", console)

    if args.save_csv:
        import os
        os.makedirs(args.save_csv, exist_ok=True)
        save_csv(combined, os.path.join(args.save_csv, "ipos.csv"))
        console.print(f"\n[dim]Saved CSV to {args.save_csv}/[/dim]")

    if args.output_html:
        save_html(ipos, args.output_html)
        console.print(f"[dim]Saved HTML dashboard to {args.output_html}[/dim]")


if __name__ == "__main__":
    main()
