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
        range_match = re.search(r"<b>(-?[\d.]+)\s*\\u2193\s*/\s*(-?[\d.]+)\s*\\u2191</b>", gmp_text)
        if not range_match:
            range_match = re.search(r"<b>(-?[\d.]+)\s*↓\s*/\s*(-?[\d.]+)\s*↑</b>", gmp_text)
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
            ipo.qib_sub = cg["qib_sub"]
            ipo.nii_sub = cg["nii_sub"]
            ipo.retail_sub = cg["retail_sub"]
            ipo.emp_sub = cg["emp_sub"]
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

    render_table(combined, f"Mainboard + SME IPOs — status={args.status}", console)

    if args.save_csv:
        import os
        os.makedirs(args.save_csv, exist_ok=True)
        save_csv(combined, os.path.join(args.save_csv, "ipos.csv"))
        console.print(f"\n[dim]Saved CSV to {args.save_csv}/[/dim]")


if __name__ == "__main__":
    main()
