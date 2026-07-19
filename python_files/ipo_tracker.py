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
import math
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
    min_lots_scraped: Optional[int] = None  # from CG page; overrides formula when set
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
    listing_open_price: Optional[float] = None
    listing_close_price: Optional[float] = None
    listing_gain_pct: Optional[float] = None
    sources: set = field(default_factory=set)
    # Broker + member recommendation counts from CG
    broker_apply: int = 0
    broker_avoid: int = 0
    broker_neutral: int = 0
    member_apply: int = 0
    member_avoid: int = 0
    member_neutral: int = 0
    # Fundamental data scraped from CG individual IPO page
    promoter_pre: Optional[float] = None   # promoter holding % pre-IPO
    promoter_post: Optional[float] = None  # promoter holding % post-IPO
    fresh_pct: Optional[float] = None      # fresh issue as % of total (0–100)
    ofs_pct: Optional[float] = None        # OFS as % of total (0–100)
    proceeds_capex: float = 0.0            # % of proceeds going to capex/expansion
    proceeds_debt: float = 0.0             # % of proceeds going to debt repayment
    proceeds_wc: float = 0.0              # % of proceeds going to working capital
    proceeds_general: float = 0.0         # % of proceeds going to general corp purposes
    # IPO Watch editorial review (Subscribe / Neutral / Avoid)
    iw_review: Optional[str] = None

    @property
    def min_lots(self) -> int:
        """Minimum lots required. Uses scraped value from CG page when available,
        else falls back to SEBI ₹1 lakh rule for SME."""
        if self.min_lots_scraped is not None:
            return self.min_lots_scraped
        if self.price_high is None or self.lot_size is None or self.price_high * self.lot_size == 0:
            return 1
        if self.category == "SME":
            return max(1, math.ceil(100_000 / (self.price_high * self.lot_size)))
        return 1

    @property
    def min_investment(self) -> Optional[float]:
        if self.price_high is None or self.lot_size is None:
            return None
        return round(self.price_high * self.lot_size * self.min_lots, 2)

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
    def expected_profit(self) -> Optional[float]:
        if self.gmp is None or self.lot_size is None:
            return None
        return round(self.gmp * self.lot_size * self.min_lots, 0)

    @property
    def roi_pct(self) -> Optional[float]:
        if self.expected_profit is None or self.min_investment is None or self.min_investment == 0:
            return None
        return round(self.expected_profit / self.min_investment * 100, 1)

    @property
    def allotment_odds(self) -> Optional[float]:
        """Retail allotment probability: 1/retail_sub × 100, capped at 100%."""
        if self.retail_sub is None or self.retail_sub <= 0:
            return None
        return round(min(1 / self.retail_sub * 100, 100), 1)

    @property
    def days_to_close(self) -> Optional[int]:
        if self.close_date is None:
            return None
        delta = (self.close_date - date.today()).days
        return delta if delta >= 0 else None

    @property
    def days_to_listing(self) -> Optional[int]:
        if self.listing_date is None:
            return None
        delta = (self.listing_date - date.today()).days
        return delta if delta >= 0 else None

    @property
    def quick_signal(self) -> str:
        """Heuristic from GMP%, subscription, ROI, and allotment odds.
        NOT a fundamentals-based call — no revenue/valuation/peer data."""
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
        if self.roi_pct is not None:
            if self.roi_pct >= 15:
                score += 1
            elif self.roi_pct < 0:
                score -= 1
        if self.allotment_odds is not None:
            if self.allotment_odds > 20:
                score += 1
            elif self.allotment_odds < 2:
                score -= 1
        if score >= 4:
            return "Strong Buy"
        if score <= 0:
            return "Do Not Buy"
        return "Not Sure, You Decide"

    @property
    def composite_score(self) -> dict:
        """
        Composite 1-5 rating. Returns dict with:
          score: float (1.0–5.0, 1 decimal)
          label: str
          factors: dict of factor_name -> {score_0_1, weight, contribution, note}
          capped_by: str or None
        """
        factors = {}

        # --- Factor 1: QIB subscription (weight 20%) ---
        if self.qib_sub is not None:
            q = self.qib_sub
            s = 1.0 if q >= 50 else 0.8 if q >= 20 else 0.6 if q >= 10 else 0.4 if q >= 5 else 0.2 if q >= 2 else 0.0
            note = f"{q:.1f}x"
        else:
            s, note = 0.3, "No data"
        factors["QIB Sub"] = {"score": s, "weight": 0.20, "note": note}

        # --- Factor 2: Overall subscription (weight 10%) ---
        strength_map = {"Exceptional": 1.0, "Strong": 0.75, "Average": 0.4, "Weak": 0.0}
        ss = self.subscription_strength
        factors["Overall Sub"] = {
            "score": strength_map.get(ss, 0.3),
            "weight": 0.10,
            "note": f"{self.overall_sub:.1f}x" if self.overall_sub else "No data"
        }

        # --- Factor 3: Allotment odds (weight 5%) ---
        if self.allotment_odds is not None:
            a = self.allotment_odds
            s = 1.0 if a > 20 else 0.75 if a > 10 else 0.5 if a > 5 else 0.25 if a > 2 else 0.0
            note = f"{a:.1f}%"
        else:
            s, note = 0.3, "No data"
        factors["Allotment Odds"] = {"score": s, "weight": 0.05, "note": note}

        # --- Factor 4: ROI % (weight 5%) ---
        if self.roi_pct is not None:
            r = self.roi_pct
            s = 1.0 if r >= 30 else 0.75 if r >= 15 else 0.5 if r >= 5 else 0.25 if r >= 0 else 0.0
            note = f"{r:.1f}%"
        else:
            s, note = 0.3, "No data"
        factors["ROI (GMP)"] = {"score": s, "weight": 0.05, "note": note}

        # --- Factor 5: GMP % (weight 10%) ---
        if self.gmp_pct is not None:
            g = self.gmp_pct
            s = 1.0 if g >= 30 else 0.75 if g >= 15 else 0.5 if g >= 5 else 0.25 if g >= 0 else 0.0
            note = f"{g:.1f}%"
        else:
            s, note = 0.3, "No data"
        factors["GMP %"] = {"score": s, "weight": 0.10, "note": note}

        # --- Factor 6: IG fire rating (weight 10%) ---
        if self.rating is not None:
            ig_map = {5: 1.0, 4: 0.75, 3: 0.5, 2: 0.25, 1: 0.0}
            s = ig_map.get(self.rating, 0.3)
            note = f"{self.rating}/5"
        else:
            s, note = 0.3, "No data"
        factors["IG Rating"] = {"score": s, "weight": 0.10, "note": note}

        # --- Factor 7: Promoter post-IPO holding (weight 15%) ---
        if self.promoter_post is not None:
            p = self.promoter_post
            s = 1.0 if p >= 70 else 0.8 if p >= 60 else 0.6 if p >= 50 else 0.4 if p >= 40 else 0.2 if p >= 30 else 0.0
            note = f"{p:.1f}%"
        else:
            s, note = 0.4, "No data"
        factors["Promoter Hold"] = {"score": s, "weight": 0.15, "note": note}

        # --- Factor 8: OFS % (weight 15%) ---
        if self.ofs_pct is not None:
            o = self.ofs_pct
            s = 1.0 if o == 0 else 0.8 if o < 20 else 0.6 if o < 40 else 0.4 if o < 60 else 0.2 if o < 80 else 0.0
            note = f"{o:.0f}% OFS"
        else:
            s, note = 0.5, "No data"
        factors["OFS %"] = {"score": s, "weight": 0.15, "note": note}

        # --- Factor 9: Use of proceeds (weight 10%) ---
        if self.fresh_pct is not None and self.fresh_pct > 0:
            # Score the proceeds allocation (out of what IS fresh)
            raw = (self.proceeds_capex * 0.9 + self.proceeds_debt * 0.5 +
                   self.proceeds_wc * 0.3 + self.proceeds_general * 0.0)
            s = min(1.0, max(0.0, raw / 100.0))
            parts = []
            if self.proceeds_capex > 5: parts.append(f"Capex {self.proceeds_capex:.0f}%")
            if self.proceeds_debt > 5: parts.append(f"Debt {self.proceeds_debt:.0f}%")
            if self.proceeds_wc > 5: parts.append(f"WC {self.proceeds_wc:.0f}%")
            if self.proceeds_general > 5: parts.append(f"GCP {self.proceeds_general:.0f}%")
            note = ", ".join(parts) if parts else "Not disclosed"
        elif self.ofs_pct is not None and self.ofs_pct >= 99:
            s, note = 0.2, "100% OFS — no fresh proceeds"
        else:
            s, note = 0.4, "No data"
        factors["Use of Proceeds"] = {"score": s, "weight": 0.10, "note": note}

        # --- Compute weighted raw score (0-1) ---
        raw = sum(f["score"] * f["weight"] for f in factors.values())
        score = round(raw * 5, 1)

        # --- Hard caps ---
        capped_by = None

        def cap(max_score: float, reason: str):
            nonlocal score, capped_by
            if score > max_score:
                score = max_score
                capped_by = reason

        if self.ofs_pct is not None and self.ofs_pct > 80:
            cap(2.0, "OFS >80%")
        if self.qib_sub is not None and self.qib_sub < 1:
            cap(2.0, "QIB <1x")
        if self.promoter_post is not None and self.promoter_post < 30:
            cap(2.0, "Promoter <30%")
        if self.proceeds_general > 80 and (self.fresh_pct or 0) > 0:
            cap(2.5, "Proceeds: mostly GCP")
        if self.gmp_pct is not None and self.gmp_pct < 0:
            cap(3.0, "Negative GMP")
        if self.iw_review == "Avoid":
            cap(2.5, "IPO Watch: Avoid")
        if self.category == "SME":
            score = round(score * 0.92, 1)  # 8% discount for SME risk
            if not capped_by:
                capped_by = "SME discount applied"

        score = max(1.0, min(5.0, score))

        label = (
            "Strong Buy" if score >= 4.5 else
            "Buy" if score >= 3.5 else
            "Research Needed" if score >= 2.5 else
            "Caution" if score >= 1.5 else
            "Avoid"
        )

        # Add contribution to each factor for display
        for f in factors.values():
            f["contribution"] = round(f["score"] * f["weight"] * 5, 2)

        return {
            "score": score,
            "label": label,
            "factors": factors,
            "capped_by": capped_by,
        }


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
# Source 1: Chittorgarh report 82 — comprehensive IPO list (primary)
# --------------------------------------------------------------------------- #

def fetch_chittorgarh_ipos(year: int) -> list[IPO]:
    """Fetch all IPOs from Chittorgarh report 82 — ~4× more coverage than
    InvestorGain, includes SME IPOs that IG misses."""
    fy = f"{year}-{str(year + 1)[-2:]}"
    url = (
        f"https://webnodejs.chittorgarh.com/cloud/report/data-read/"
        f"82/1/6/{year}/{fy}/0/all/0"
    )
    try:
        resp = requests.get(url, headers=CG_HEADERS, params={"search": ""}, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("reportTableData", [])
    except (requests.RequestException, ValueError):
        return []

    ipos = []
    for row in rows:
        # ~IPO field gives clean "Company Name IPO" string
        raw_name = row.get("~IPO", "")
        if not raw_name:
            company_html = row.get("Company", "")
            raw_name = strip_html(re.sub(r'<span[^>]*>.*?</span>', '', company_html))
        name = re.sub(r"\s+IPO\s*$", "", raw_name, flags=re.IGNORECASE).strip()
        name = re.sub(r"\s+(Ltd\.?|Limited)\.?\s*$", "", name).strip()
        if not name:
            continue

        cat_str = row.get("Issue Category", "").strip()
        category = "Mainboard" if cat_str in ("IPO", "Mainboard") else "SME"

        # Price band: "161.00 to 170.00" or single "149.00"
        price_str = str(row.get("Issue Price (Rs.)", "") or "")
        price_low, price_high = None, None
        pm = re.search(r'([\d.]+)\s*(?:to|-)\s*([\d.]+)', price_str)
        if pm:
            price_low, price_high = float(pm.group(1)), float(pm.group(2))
        else:
            pm2 = re.search(r'([\d.]+)', price_str)
            if pm2:
                price_low = price_high = float(pm2.group(1))

        open_date = parse_iso_date(row.get("~Issue_Open_Date"))
        close_date = parse_iso_date(row.get("~IssueCloseDate"))
        listing_date = parse_iso_date(row.get("~ListingDate"))

        issue_cr = None
        try:
            amt = row.get("Total Issue Amount (Incl.Firm reservations) (Rs.cr.)")
            if amt:
                issue_cr = float(amt)
        except (ValueError, TypeError):
            pass

        status = compute_status(open_date, close_date)

        slug = row.get("~URLRewrite_Folder_Name", "")
        # ID is in the Company HTML href: /ipo/slug/ID/
        company_html = row.get("Company", "")
        id_match = re.search(r'/ipo/[^/]+/(\d+)/', company_html)
        cg_id = id_match.group(1) if id_match else ""

        ipo = IPO(
            name=name,
            category=category,
            status=status,
            open_date=open_date,
            close_date=close_date,
            listing_date=listing_date,
            price_low=price_low,
            price_high=price_high,
            issue_size_cr=issue_cr,
        )
        # Store slug+id so scrape_lot_size can build the correct URL
        ipo._cg_slug = f"{slug}/{cg_id}" if slug and cg_id else slug  # type: ignore[attr-defined]
        ipo.sources.add("chittorgarh")
        ipos.append(ipo)

    return ipos


# --------------------------------------------------------------------------- #
# Source 2: InvestorGain — GMP + lot size lookup (supplement)
# --------------------------------------------------------------------------- #

def fetch_investorgain_lookup(year: int) -> dict[str, dict]:
    """Returns a dict keyed by normalize_name(company) with GMP, lot size,
    overall subscription, rating from InvestorGain (~30 recent IPOs)."""
    fy = f"{year}-{str(year + 1)[-2:]}"
    url = (
        f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/"
        f"331/1/6/{year}/{fy}/0/all"
    )
    try:
        resp = requests.get(url, headers=IG_HEADERS, params={"search": ""}, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("reportTableData", [])
    except (requests.RequestException, ValueError):
        return {}

    result = {}
    for row in rows:
        name = row.get("~ipo_name", "").strip()
        if not name:
            continue
        key = normalize_name(name)

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

        lot = row.get("Lot")
        try:
            lot = int(lot) if lot else None
        except (ValueError, TypeError):
            lot = None

        rating_html = row.get("Rating", "")
        rating = rating_html.count("&#128293;") or None

        result[key] = {
            "gmp": gmp,
            "gmp_pct": gmp_pct,
            "gmp_low": gmp_low,
            "gmp_high": gmp_high,
            "lot_size": lot,
            "overall_sub": overall_sub,
            "rating": rating,
            "anchor": "check" in row.get("Anchor", "").lower() or "✅" in row.get("Anchor", ""),
            "pe": float(row["~P/E"]) if row.get("~P/E") not in (None, "--", "") else None,
            "boa_date": parse_iso_date(row.get("~Srt_BoA_Dt")),
        }
    return result


# --------------------------------------------------------------------------- #
# Source 3: Chittorgarh subscription breakdown (mainboard + sme, two calls)
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
            "total_sub": row.get("Total (x)") or None,
            "applications": row.get("Applications") or None,
        }
    return result


# --------------------------------------------------------------------------- #
# Source 4: Chittorgarh report 98 — listing day performance
# --------------------------------------------------------------------------- #

def fetch_chittorgarh_listing(year: int) -> dict[str, dict]:
    """Returns listing open/close price and gain% keyed by normalize_name.
    Uses CG report 98 which covers ~117 IPOs including historical ones."""
    fy = f"{year}-{str(year + 1)[-2:]}"
    url = (
        f"https://webnodejs.chittorgarh.com/cloud/report/data-read/"
        f"98/1/6/{year}/{fy}/0/all/0"
    )
    try:
        resp = requests.get(url, headers=CG_HEADERS, params={"search": ""}, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("reportTableData", [])
    except (requests.RequestException, ValueError):
        return {}

    result = {}
    for row in rows:
        name = row.get("Company", "").strip()
        name = re.sub(r"\s+(Ltd\.?|Limited)\.?\s*$", "", name).strip()
        key = normalize_name(name)

        def _fp(v: object) -> Optional[float]:
            try:
                return float(v) if v and str(v).strip() else None
            except (ValueError, TypeError):
                return None

        open_p = _fp(row.get("Open Price on Listing (Rs.)"))
        close_p = _fp(row.get("Close Price on Listing (Rs.)"))

        gain_raw = strip_html(row.get("% Gain / Loss (Issue price v/s Close price on Listing)", "") or "")
        gain_pct = None
        if gain_raw:
            try:
                v = float(gain_raw)
                gain_pct = v if v != 0 or close_p is not None else None
            except (ValueError, TypeError):
                pass

        if open_p is not None or gain_pct is not None:
            result[key] = {
                "listing_open_price": open_p,
                "listing_close_price": close_p,
                "listing_gain_pct": gain_pct,
            }
    return result


# --------------------------------------------------------------------------- #
# Source 5: Chittorgarh individual page — lot size scraper (for closed IPOs)
# --------------------------------------------------------------------------- #

def scrape_broker_recs(slug: str) -> dict:
    """Scrape broker + member recommendation counts from Chittorgarh recommendation page.
    slug format: 'kratikal-tech-ipo/2096'
    Returns dict with keys: broker_apply, broker_avoid, broker_neutral,
                             member_apply, member_avoid, member_neutral"""
    try:
        parts = slug.split("/")
        if len(parts) < 2:
            return {}
        name_slug, cg_id = parts[0], parts[1]
        url = f"https://www.chittorgarh.com/ipo-recommendation/{name_slug}/{cg_id}/"
        resp = requests.get(url, headers=CG_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text[:250000]

        result = {}

        # CG recommendation page has two tables: Brokers then Members
        # Headers: Review By | Apply | May Apply | Neutral | Avoid | Not Rated
        # Count row has numeric <td> values

        tables = re.findall(r'(?:Broker|Member).*?</table>', html, re.DOTALL | re.IGNORECASE)
        for table in tables[:2]:
            header_text = table[:200].lower()
            # Extract all numeric td values from the Count row
            nums = re.findall(r'<td[^>]*>(\d+)</td>', table)
            if len(nums) < 4:
                continue
            # nums order matches header: Apply, May Apply, Neutral, Avoid, Not Rated
            total_apply  = int(nums[0]) + int(nums[1])  # Apply + May Apply
            total_neutral = int(nums[2])
            total_avoid   = int(nums[3])

            if 'broker' in header_text:
                result['broker_apply']   = total_apply
                result['broker_neutral'] = total_neutral
                result['broker_avoid']   = total_avoid
            elif 'member' in header_text:
                result['member_apply']   = total_apply
                result['member_neutral'] = total_neutral
                result['member_avoid']   = total_avoid

        return result
    except (requests.RequestException, ValueError, IndexError):
        return {}


def scrape_ipo_fundamentals(slug: str) -> dict:
    """
    Scrape promoter holding, OFS/fresh split, and use of proceeds from CG individual IPO page.
    slug format: 'kratikal-tech-ipo/2844'
    """
    try:
        parts = slug.split("/")
        if len(parts) < 2:
            return {}
        url = f"https://www.chittorgarh.com/ipo/{parts[0]}/{parts[1]}/"
        resp = requests.get(url, headers=CG_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text

        result = {}

        # --- Extract JSON fields from RSC payload ---
        def json_val(key: str) -> Optional[str]:
            m = re.search(
                r'\\\"' + re.escape(key) + r'\\\":(\\\"([^\\\"]{0,200})\\\"|(-?[\d.]+)|null)',
                html
            )
            if not m:
                return None
            if m.group(2) is not None:
                return m.group(2).strip()
            if m.group(3) is not None:
                return m.group(3).strip()
            return None

        # Promoter holding
        pre_str  = json_val("promoter_shareholding_pre_issue")
        post_str = json_val("promoter_shareholding_post_issue")
        if pre_str:
            m = re.search(r"([\d.]+)", pre_str)
            if m: result["promoter_pre"] = float(m.group(1))
        if post_str:
            m = re.search(r"([\d.]+)", post_str)
            if m: result["promoter_post"] = float(m.group(1))

        # Fresh / OFS split
        fresh_str = json_val("issue_size_fresh_in_amt")
        ofs_str   = json_val("issue_size_ofs_in_amt")
        try:
            fresh_amt = float(fresh_str or 0)
            ofs_amt   = float(ofs_str or 0)
            total_amt = fresh_amt + ofs_amt
            if total_amt > 0:
                result["fresh_pct"] = round(fresh_amt / total_amt * 100, 1)
                result["ofs_pct"]   = round(ofs_amt   / total_amt * 100, 1)
        except (ValueError, TypeError):
            pass

        # Use of proceeds — parse "Objects of the Issue" section
        idx = html.lower().find("objects of the issue")
        if idx >= 0:
            section = html[idx: idx + 4000]
            text = re.sub(r"<[^>]+>", " ", section)
            text = re.sub(r"\s+", " ", text).lower()

            # Identify which categories appear
            has_capex   = bool(re.search(r"capital expenditure|capex|purchase of|product dev|invest|marketing|technology|r&d|research", text))
            has_debt    = bool(re.search(r"repayment|prepayment|debt|borrow|loan", text))
            has_wc      = bool(re.search(r"working capital", text))
            has_general = bool(re.search(r"general corporate", text))

            # Assign amounts to categories by keyword proximity
            capex_amt = debt_amt = wc_amt = gen_amt = 0.0
            lines = text.split(".")
            for line in lines:
                nums = re.findall(r"\b(\d+(?:\.\d+)?)\b", line)
                if not nums:
                    continue
                try:
                    amt = float(nums[-1])
                except ValueError:
                    continue
                if amt < 0.1 or amt > 10000:
                    continue
                if re.search(r"capital expenditure|capex|purchase of|product dev|invest|marketing|technology|r&d|research|subsidiar", line):
                    capex_amt += amt
                elif re.search(r"repayment|prepayment|debt|borrow|loan", line):
                    debt_amt += amt
                elif re.search(r"working capital", line):
                    wc_amt += amt
                elif re.search(r"general corporate", line):
                    gen_amt += amt

            total_proceeds = capex_amt + debt_amt + wc_amt + gen_amt
            if total_proceeds > 0:
                result["proceeds_capex"]   = round(capex_amt / total_proceeds * 100, 1)
                result["proceeds_debt"]    = round(debt_amt  / total_proceeds * 100, 1)
                result["proceeds_wc"]      = round(wc_amt    / total_proceeds * 100, 1)
                result["proceeds_general"] = round(gen_amt   / total_proceeds * 100, 1)
            else:
                # Fallback: assign 100% to whichever category is present
                if has_capex and not has_general:
                    result["proceeds_capex"] = 100.0
                elif has_debt and not has_general:
                    result["proceeds_debt"] = 100.0
                elif has_wc and not has_general:
                    result["proceeds_wc"] = 100.0
                elif has_general:
                    result["proceeds_general"] = 100.0

        return result
    except (requests.RequestException, ValueError):
        return {}


def scrape_lot_size(slug: str) -> tuple[Optional[int], Optional[int]]:
    """Scrape lot size and retail minimum lots from Chittorgarh individual IPO page.
    Returns (lot_size, min_lots_retail). min_lots_retail is None when not found."""
    try:
        url = f"https://www.chittorgarh.com/ipo/{slug}/"
        resp = requests.get(url, headers=CG_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        text = resp.text[:100000]
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean)

        lot_size: Optional[int] = None
        min_lots_retail: Optional[int] = None

        # Lot size: "lot size for an application is 2,000 shares"
        m = re.search(r'[Ll]ot\s+[Ss]ize.*?>([\d,]+)\s*[Ss]hares?', text, re.DOTALL)
        if not m:
            m = re.search(r'lot size for an application is ([\d,]+)\s*shares?', clean, re.IGNORECASE)
        if not m:
            m = re.search(r'minimum of ([\d,]+)\s*[Ss]hares?', clean, re.IGNORECASE)
        if m:
            lot_size = int(m.group(1).replace(",", ""))

        # Retail minimum: "minimum amount... ₹X,XX,XXX (N shares)"
        rm = re.search(
            r'minimum amount[^₹]*₹[\d,]+\s*\(([\d,]+)\s*shares?\)',
            clean, re.IGNORECASE
        )
        if rm and lot_size:
            min_shares = int(rm.group(1).replace(",", ""))
            min_lots_retail = max(1, math.ceil(min_shares / lot_size))

        return lot_size, min_lots_retail
    except (requests.RequestException, ValueError):
        pass
    return None, None


def scrape_ipowatch_review(slug: str) -> Optional[str]:
    """Scrape editorial review verdict from ipowatch.in.
    slug format: 'kratikal-tech-ipo/2844' — uses the name part before '/'
    Returns 'Subscribe', 'Neutral', 'Avoid', or None.
    """
    try:
        name_slug = slug.split("/")[0]
        url = f"https://ipowatch.in/{name_slug}/"
        resp = requests.get(url, headers=CG_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        m = re.search(r'<li><strong>Review:</strong>\s*([^<]+)</li>', resp.text, re.IGNORECASE)
        if m:
            verdict = m.group(1).strip()
            # Normalise to canonical values
            vl = verdict.lower()
            if "avoid" in vl:
                return "Avoid"
            if "subscribe" in vl:
                return "Subscribe"
            if "neutral" in vl:
                return "Neutral"
            return verdict  # return raw if unrecognised
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return None


# --------------------------------------------------------------------------- #
# Source 6: Groww IPO page (__NEXT_DATA__ embedded JSON) — kept for reference
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
    # Primary: CG report 82 — ~120 IPOs including SME ones InvestorGain misses
    ipos = fetch_chittorgarh_ipos(year)

    # GMP + lot size supplement from InvestorGain (~30 recent IPOs)
    ig_lookup = fetch_investorgain_lookup(year)

    # Subscription breakdown (QIB / NII / Retail / Total)
    cg_sub = {
        **fetch_chittorgarh_subscription(year, "mainboard"),
        **fetch_chittorgarh_subscription(year, "sme"),
    }

    # Listing day performance (open price, close price, gain%)
    cg_listing = fetch_chittorgarh_listing(year)

    def _f(v: object) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    for ipo in ipos:
        key = normalize_name(ipo.name)

        ig = ig_lookup.get(key)
        if ig:
            ipo.gmp = ig.get("gmp")
            ipo.gmp_pct = ig.get("gmp_pct")
            ipo.gmp_low = ig.get("gmp_low")
            ipo.gmp_high = ig.get("gmp_high")
            if ig.get("lot_size"):
                ipo.lot_size = ig["lot_size"]
            if ipo.overall_sub is None:
                ipo.overall_sub = ig.get("overall_sub")
            ipo.rating = ig.get("rating")
            ipo.anchor = ig.get("anchor", False)
            ipo.pe = ig.get("pe")
            if ipo.boa_date is None:
                ipo.boa_date = ig.get("boa_date")
            ipo.sources.add("investorgain")

        sub = cg_sub.get(key)
        if sub:
            ipo.qib_sub = _f(sub["qib_sub"])
            ipo.nii_sub = _f(sub["nii_sub"])
            ipo.retail_sub = _f(sub["retail_sub"])
            ipo.emp_sub = _f(sub["emp_sub"])
            ipo.applications = sub["applications"]
            if ipo.overall_sub is None:
                ipo.overall_sub = _f(sub["total_sub"])

        lst = cg_listing.get(key)
        if lst:
            ipo.listing_open_price = lst.get("listing_open_price")
            ipo.listing_close_price = lst.get("listing_close_price")
            ipo.listing_gain_pct = lst.get("listing_gain_pct")

    # Scrape lot size from individual CG pages for recently closed IPOs that
    # have listing data but no lot size (so we can show ₹ profit, not just %).
    # Limit to IPOs closed within last 45 days to keep runtime reasonable.
    cutoff = date.today().replace(day=max(1, date.today().day - 45))
    needs_lot = [
        ipo for ipo in ipos
        if ipo.lot_size is None
        and ipo.listing_gain_pct is not None
        and ipo.close_date is not None
        and ipo.close_date >= cutoff
        and getattr(ipo, "_cg_slug", "")
    ]
    for ipo in needs_lot:
        slug = getattr(ipo, "_cg_slug", "")
        if slug:
            lot, min_lots_r = scrape_lot_size(slug)
            if lot:
                ipo.lot_size = lot
            if min_lots_r is not None:
                ipo.min_lots_scraped = min_lots_r

    # Scrape min lots + fundamentals for Open/Upcoming IPOs
    for ipo in ipos:
        if ipo.status not in ("Open", "Upcoming"):
            continue
        slug = getattr(ipo, "_cg_slug", "")
        if not slug:
            continue
        if ipo.lot_size is None or ipo.min_lots_scraped is None:
            lot, min_lots_r = scrape_lot_size(slug)
            if lot and ipo.lot_size is None:
                ipo.lot_size = lot
            if min_lots_r is not None:
                ipo.min_lots_scraped = min_lots_r

        data = scrape_ipo_fundamentals(slug)
        if data.get("promoter_pre")  is not None: ipo.promoter_pre  = data["promoter_pre"]
        if data.get("promoter_post") is not None: ipo.promoter_post = data["promoter_post"]
        if data.get("fresh_pct")     is not None: ipo.fresh_pct     = data["fresh_pct"]
        if data.get("ofs_pct")       is not None: ipo.ofs_pct       = data["ofs_pct"]
        ipo.proceeds_capex   = data.get("proceeds_capex",   0.0)
        ipo.proceeds_debt    = data.get("proceeds_debt",    0.0)
        ipo.proceeds_wc      = data.get("proceeds_wc",      0.0)
        ipo.proceeds_general = data.get("proceeds_general", 0.0)

    # Scrape IPO Watch editorial review for Open/Upcoming IPOs
    for ipo in ipos:
        if ipo.status not in ("Open", "Upcoming"):
            continue
        slug = getattr(ipo, "_cg_slug", "")
        if not slug:
            continue
        review = scrape_ipowatch_review(slug)
        if review:
            ipo.iw_review = review

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
        "minLots":     ipo.min_lots,
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
        "applications":  ipo.applications,
        "sources":       sorted(ipo.sources),
        "expectedProfit": ipo.expected_profit,
        "roiPct":        ipo.roi_pct,
        "allotmentOdds": ipo.allotment_odds,
        "daysToClose":   ipo.days_to_close,
        "daysToListing": ipo.days_to_listing,
        "listingOpenPrice":  ipo.listing_open_price,
        "listingClosePrice": ipo.listing_close_price,
        "listingGainPct":    ipo.listing_gain_pct,
        "brokerApply":   ipo.broker_apply,
        "brokerAvoid":   ipo.broker_avoid,
        "brokerNeutral": ipo.broker_neutral,
        "memberApply":   ipo.member_apply,
        "memberAvoid":   ipo.member_avoid,
        "memberNeutral": ipo.member_neutral,
        "promoterPre":    ipo.promoter_pre,
        "promoterPost":   ipo.promoter_post,
        "freshPct":       ipo.fresh_pct,
        "ofsPct":         ipo.ofs_pct,
        "proceedsCapex":  ipo.proceeds_capex,
        "proceedsDebt":   ipo.proceeds_debt,
        "proceedsWc":     ipo.proceeds_wc,
        "proceedsGeneral":ipo.proceeds_general,
        "iwReview":       ipo.iw_review,
        "compositeScore": ipo.composite_score["score"],
        "compositeLabel": ipo.composite_score["label"],
        "compositeFactors": {k: {"score": v["score"], "weight": v["weight"], "contribution": v["contribution"], "note": v["note"]} for k, v in ipo.composite_score["factors"].items()},
        "compositeCappedBy": ipo.composite_score["capped_by"],
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
.updated{font-size:11px;font-family:var(--mono);color:var(--text-3);display:flex;align-items:center;gap:6px;line-height:1.3}
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
.refresh-btn{display:flex;align-items:center;gap:5px;background:var(--bg);border:1px solid var(--border);color:var(--text-2);font-size:11.5px;padding:4px 10px;border-radius:5px;cursor:pointer;font-family:var(--sans);transition:background .15s,color .15s,border-color .15s}
.refresh-btn:hover{background:var(--surface-2);color:var(--text);border-color:var(--accent)}
.refresh-btn svg{transition:transform .5s}
.refresh-btn.spinning svg{animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-8px)}40%{transform:translateX(8px)}60%{transform:translateX(-6px)}80%{transform:translateX(6px)}}
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
.sig-promise{color:var(--green)}.sig-buy{color:#16a34a}.sig-neutral{color:var(--amber)}.sig-caution{color:var(--red)}
.str-exc{color:var(--green);font-weight:700}.str-str{color:#15803D}.str-avg{color:var(--amber)}.str-wk{color:var(--red)}
.tag{display:inline-flex;align-items:center;padding:1px 6px;border-radius:3px;font-size:10px;font-family:var(--sans);font-weight:600}
.tag-pos{background:rgba(21,128,61,.1);color:var(--green)}.tag-neg{background:rgba(185,28,28,.1);color:var(--red)}.tag-dim{background:rgba(148,163,184,.15);color:var(--text-3)}
.empty-row td{text-align:center;padding:28px;color:var(--text-3);font-family:var(--sans);font-style:italic;font-size:12px}
.sent-wrap{display:flex;flex-direction:column;gap:3px;min-width:120px}
.sent-row{display:flex;align-items:center;gap:5px;font-size:10px;font-family:var(--sans)}
.sent-lbl{color:var(--text-3);min-width:34px;font-size:9px;text-transform:uppercase;letter-spacing:.05em}
.sent-bar-wrap{flex:1;height:5px;background:var(--border);border-radius:3px;overflow:hidden;min-width:50px}
.sent-bar-fill{height:100%;border-radius:3px;transition:width .3s}
.sent-counts{font-size:9.5px;white-space:nowrap}
.sent-fire{color:var(--amber);letter-spacing:1px}
.snt-pos{color:var(--green)}.snt-neg{color:var(--red)}.snt-dim{color:var(--text-3)}
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
<div id="app">
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
  <div style="display:flex;align-items:center;gap:6px;margin-left:auto">
    <svg style="width:14px;height:14px;color:var(--text-3);flex-shrink:0" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd"/></svg>
    <input type="text" id="search-input" placeholder="Search IPO name..." oninput="setSearch(this.value)"
      style="border:1px solid var(--border-2);border-radius:6px;padding:5px 10px;font-size:13px;color:var(--text);background:var(--surface);outline:none;width:180px">
  </div>
  <button class="refresh-btn" id="refresh-btn" onclick="doRefresh()" title="Reload page to get latest data">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
    Refresh
  </button>
  <span id="ctrl-stats" style="margin-left:8px;white-space:nowrap"></span>
</div>
<div id="content"></div>
<script>
var allIpos=[],statusFilter='open',sortBy='gmp',searchQuery='';
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
  if(ipo.compositeScore!==null&&ipo.compositeScore!==undefined)return ipo.compositeLabel||'-';
  if(ipo.overallSub===null)return'-';
  var sc={Weak:0,Average:1,Strong:2,Exceptional:3}[subStr(ipo)]||0;
  if(ipo.gmpPct!==null){if(ipo.gmpPct<0)sc-=2;else if(ipo.gmpPct>=20)sc+=2;else if(ipo.gmpPct>=5)sc+=1;}
  return sc>=3?'Strong Buy':sc<=0?'Do Not Buy':'Not Sure, You Decide';
}

var fmtMoney=function(v){return v!==null?'\\u20b9'+Math.round(v).toLocaleString('en-IN'):'-';};
var fmtSub=function(v){return v!==null?parseFloat(v).toFixed(1)+'x':'-';};
function fmtNum(v,dec){return v!==null&&v!==undefined?parseFloat(v).toFixed(dec||0):'-';}

function sPill(s){var c={Open:'p-open',Upcoming:'p-upcoming',Closed:'p-closed'}[s]||'p-closed';return'<span class="pill '+c+'">'+s+'</span>';}

function gmpCell(ipo){
  // For closed IPOs with no GMP: show actual listing gain% instead
  if(ipo.gmp===null){
    if(ipo.status==='Closed'&&ipo.listingGainPct!==null&&ipo.listingGainPct!==undefined){
      var g=parseFloat(ipo.listingGainPct);
      var gc=g>0?'pos':g<0?'neg':'';
      var prefix=g>0?'+':'';
      return'<span class="'+gc+'" title="Actual listing day gain">'
        +prefix+g.toFixed(1)+'% <span style="font-size:10px;color:var(--text-3)">(listed)</span></span>';
    }
    return'<span class="dim">-</span>';
  }
  var cls=ipo.gmp>0?'pos':ipo.gmp<0?'neg':'';
  var pct=ipo.gmpPct!==null?' <span style="color:var(--text-3);font-size:11px">('+parseFloat(ipo.gmpPct).toFixed(1)+'%)</span>':'';
  return'<span class="'+cls+'">\\u20b9'+parseFloat(ipo.gmp).toFixed(0)+pct+'</span>';
}

function subCell(ipo){
  if(ipo.overallSub===null)return'<span class="dim">-</span>';
  var s=subStr(ipo);var c={Exceptional:'str-exc',Strong:'str-str',Average:'str-avg',Weak:'str-wk'}[s]||'';
  return'<span class="'+c+'">'+parseFloat(ipo.overallSub).toFixed(1)+'x</span>';
}

function listingProfit(ipo){
  // Actual profit on listing: listing_gain_pct% of (issue price × lot size)
  // Returns null when lot size unknown — caller will show gain% instead
  if(ipo.listingGainPct===null||ipo.listingGainPct===undefined)return null;
  var mi=minInv(ipo);
  if(mi===null)return null;
  return(ipo.listingGainPct/100)*mi;
}

function profitCell(ipo){
  // Closed IPOs: show actual listing profit if available, else fall back to GMP-based
  if(ipo.status==='Closed'){
    var lp=listingProfit(ipo);
    if(lp!==null){
      var cls=lp>0?'tag tag-pos':lp<0?'tag tag-neg':'';
      return'<span class="'+cls+'" title="Actual listing day profit">'+(lp>0?'+':'')+fmtMoney(Math.round(lp))+'</span>';
    }
    // Listing gain% available but no lot size — show %
    if(ipo.listingGainPct!==null&&ipo.listingGainPct!==undefined){
      var g=parseFloat(ipo.listingGainPct);
      return'<span class="'+(g>0?'pos':g<0?'neg':'')+'" title="Listing gain % — lot size unavailable">'+(g>0?'+':'')+g.toFixed(1)+'%</span>';
    }
    // Not listed yet — fall back to GMP-based expected profit (same as Open/Upcoming)
    if(ipo.expectedProfit!==null&&ipo.expectedProfit!==undefined){
      var v=parseFloat(ipo.expectedProfit);
      return'<span class="'+(v>0?'tag tag-pos':v<0?'tag tag-neg':'')+'" title="Expected profit based on GMP (not yet listed)">'+(v>0?'+':'')+fmtMoney(v)+'</span>';
    }
    return'<span class="dim">-</span>';
  }
  if(ipo.expectedProfit===null||ipo.expectedProfit===undefined)return'<span class="dim">-</span>';
  var v=parseFloat(ipo.expectedProfit);
  return'<span class="'+(v>0?'tag tag-pos':v<0?'tag tag-neg':'')+'">'+(v>0?'+':'')+fmtMoney(v)+'</span>';
}

function roiCell(ipo){
  // Closed IPOs: actual ROI = listing_gain_pct; if not listed yet, fall back to GMP-based
  if(ipo.status==='Closed'){
    if(ipo.listingGainPct!==null&&ipo.listingGainPct!==undefined){
      var g=parseFloat(ipo.listingGainPct);
      return'<span class="'+(g>0?'pos':g<0?'neg':'')+'" style="font-weight:600" title="Actual listing day ROI">'+(g>0?'+':'')+g.toFixed(1)+'%</span>';
    }
    // Not listed yet — show GMP-based ROI
    if(ipo.roiPct!==null&&ipo.roiPct!==undefined){
      var v=parseFloat(ipo.roiPct);
      return'<span class="'+(v>0?'pos':v<0?'neg':'')+'" style="font-weight:600" title="Expected ROI based on GMP (not yet listed)">'+v.toFixed(1)+'%</span>';
    }
    return'<span class="dim">-</span>';
  }
  if(ipo.roiPct===null||ipo.roiPct===undefined)return'<span class="dim">-</span>';
  var v=parseFloat(ipo.roiPct);
  return'<span class="'+(v>0?'pos':v<0?'neg':'')+'" style="font-weight:600">'+v.toFixed(1)+'%</span>';
}

function oddsCell(ipo){
  if(ipo.allotmentOdds===null||ipo.allotmentOdds===undefined)return'<span class="dim">-</span>';
  var v=parseFloat(ipo.allotmentOdds);
  return'<span class="'+(v>15?'pos':v<3?'neg':'amb')+'">'+v.toFixed(1)+'%</span>';
}

function sigCell(ipo){
  var s=quickSig(ipo);if(s==='-')return'<span class="dim">-</span>';
  var score=ipo.compositeScore;
  var c={
    'Strong Buy':'sig-promise','Buy':'sig-buy',
    'Research Needed':'sig-neutral','Caution':'sig-caution',
    'Avoid':'sig-caution','Do Not Buy':'sig-caution',
    'Not Sure, You Decide':'sig-neutral'
  }[s]||'sig-neutral';
  var scoreStr=score!=null?'<span style="font-size:10px;opacity:.7;margin-left:4px">'+parseFloat(score).toFixed(1)+'/5</span>':'';
  // Build tooltip from factors
  var tip='';
  if(ipo.compositeFactors){
    var rows=Object.entries(ipo.compositeFactors).map(function(e){
      var bars=Math.round(e[1].score*5);
      var bar='';for(var i=0;i<5;i++)bar+=(i<bars?'█':'░');
      return e[0]+': '+bar+' '+e[1].note;
    });
    tip=rows.join('&#10;');
    if(ipo.compositeCappedBy)tip+='&#10;⚠ Capped: '+ipo.compositeCappedBy;
  }
  return'<span class="sig '+c+'" title="'+tip+'">'+s+scoreStr+'</span>';
}

function trendCell(ipo){
  var t=gmpTrend(ipo);if(t==='-')return'<span class="dim">-</span>';
  var arr={Rising:'\\u2191',Falling:'\\u2193',Flat:'\\u2192',Mixed:'\\u2248'}[t]||'';
  var c={Rising:'pos',Falling:'neg',Flat:'dim',Mixed:'amb'}[t]||'';
  return'<span class="'+c+'">'+arr+' '+t+'</span>';
}

function priceBand(ipo){
  if(!ipo.priceLow&&!ipo.priceHigh)return'-';
  if(ipo.priceLow&&ipo.priceHigh&&ipo.priceLow!==ipo.priceHigh)return'\\u20b9'+parseFloat(ipo.priceLow).toFixed(0)+'\\u2013'+parseFloat(ipo.priceHigh).toFixed(0);
  return'\\u20b9'+parseFloat(ipo.priceHigh||ipo.priceLow).toFixed(0);
}

function ratingCell(ipo){
  return ipo.rating!==null?'<span style="color:var(--amber);font-weight:600">'+ipo.rating+'</span><span style="color:var(--text-3)">/5</span>':'<span class="dim">-</span>';
}

function sentimentCell(ipo){
  var html='<div class="sent-wrap">';
  var fires=ipo.rating||0;

  // IG rating row — numeric X/5
  var igCol=fires>=4?'var(--green)':fires>=3?'var(--amber)':fires>0?'var(--red)':'var(--text-3)';
  html+='<div class="sent-row" title="InvestorGain editorial rating (1-5)">'
    +'<span class="sent-lbl">IG</span>'
    +'<span style="font-weight:600;color:'+igCol+'">'+fires+'</span>'
    +'<span style="color:var(--text-3)">/5</span>'
    +'</div>';

  // IPO Watch editorial review row
  if(ipo.iwReview){
    var iwCol=ipo.iwReview==='Subscribe'?'var(--green)':ipo.iwReview==='Avoid'?'var(--red)':'var(--amber)';
    html+='<div class="sent-row" title="IPO Watch editorial review (ipowatch.in)">'
      +'<span class="sent-lbl">IW</span>'
      +'<span style="font-weight:600;color:'+iwCol+';font-size:9px">'+ipo.iwReview+'</span>'
      +'</div>';
  }

  // Broker coverage badge
  var bTotal=ipo.brokerApply+ipo.brokerNeutral+ipo.brokerAvoid;
  if(bTotal>0){
    var bDom=ipo.brokerAvoid>ipo.brokerApply?'Avoid':ipo.brokerApply>ipo.brokerAvoid?'Apply':'Neutral';
    var bCls=bDom==='Avoid'?'snt-neg':bDom==='Apply'?'snt-pos':'snt-dim';
    html+='<div class="sent-row" title="Broker analyst recommendations from Chittorgarh">'
      +'<span class="sent-lbl">Broker</span>'
      +'<span class="'+bCls+'" style="font-size:9px">\\u2713'+ipo.brokerApply+' Apply / \\u2717'+ipo.brokerAvoid+' Avoid</span>'
      +'</div>';
  } else {
    html+='<div class="sent-row" title="No broker analyst coverage available">'
      +'<span class="sent-lbl">Broker</span>'
      +'<span class="snt-dim" style="font-size:9px">Not covered</span>'
      +'</div>';
  }

  // SME risk badge
  if(ipo.category==='SME'){
    html+='<div class="sent-row">'
      +'<span class="sent-lbl" style="min-width:34px"></span>'
      +'<span style="font-size:9px;background:rgba(180,83,9,.1);color:#B45309;padding:1px 5px;border-radius:3px;font-family:var(--sans);font-weight:600">SME \\u26a0 Higher risk</span>'
      +'</div>';
  }

  html+='</div>';
  return html;
}

function closingCell(ipo){
  var d=fmtDate(ipo.closeDate);
  if(ipo.daysToClose===0)return d+' <span class="tag tag-neg">Today</span>';
  if(ipo.daysToClose===1)return d+' <span class="tag tag-neg">Tomorrow</span>';
  return d;
}

function applyFilters(ipos){
  var q=searchQuery.toLowerCase().trim();
  return ipos.filter(function(i){
    if(statusFilter!=='all'&&i.status.toLowerCase()!==statusFilter)return false;
    if(q&&i.name.toLowerCase().indexOf(q)===-1)return false;
    return true;
  });
}
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
      +'<div class="metric"><span class="metric-lbl">'+(ipo.status==='Closed'?'Listing Profit':'Exp. Profit')+'</span><span class="metric-val">'+profitCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">ROI %</span><span class="metric-val">'+roiCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">'+(ipo.status==='Closed'?'Listing Gain':'GMP')+'</span><span class="metric-val">'+gmpCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Allotment Odds</span><span class="metric-val">'+oddsCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Subscription</span><span class="metric-val">'+subCell(ipo)+'</span></div>'
      +'<div class="metric"><span class="metric-lbl">Min Invest</span><span class="metric-val">'+(mi!==null?fmtMoney(mi):'-')+'</span></div>'
      +'</div>'
      +'<div style="padding-top:8px;border-top:1px solid var(--border)">'+sentimentCell(ipo)+'</div>'
      +'<div class="card-footer"><span class="card-day">'+dayLabel(ipo)+' \\u00b7 Closes '+closingCell(ipo)+'</span>'+sigCell(ipo)+'</div>'
      +'</div>';
  }).join('');
}

function renderSection(ipos,category){
  var list=sortIpos(applyFilters(ipos.filter(function(i){return i.category===category;})));
  var color=category==='Mainboard'?'var(--accent)':'var(--violet)';
  var cols=17;
  var rows=list.length===0
    ?'<tr class="empty-row"><td colspan="'+cols+'">No '+category+' IPOs match current filters.</td></tr>'
    :list.map(function(ipo){
      var mi=minInv(ipo);
      return'<tr>'
        +'<td class="td-name" title="'+ipo.name+'">'+ipo.name+'</td>'
        +'<td>'+sPill(ipo.status)+'</td>'
        +'<td style="color:var(--text-3)">'+dayLabel(ipo)+'</td>'
        +'<td>'+closingCell(ipo)+'</td>'
        +'<td>'+fmtDate(ipo.listingDate)+'</td>'
        +'<td>'+priceBand(ipo)+'</td>'
        +'<td>'+(mi!==null?fmtMoney(mi):'-')+'</td>'
        +'<td>'+subCell(ipo)+'</td>'
        +'<td>'+fmtSub(ipo.qibSub)+'</td>'
        +'<td>'+fmtSub(ipo.retailSub)+'</td>'
        +'<td>'+oddsCell(ipo)+'</td>'
        +'<td>'+gmpCell(ipo)+'</td>'
        +'<td>'+trendCell(ipo)+'</td>'
        +'<td>'+profitCell(ipo)+'</td>'
        +'<td>'+roiCell(ipo)+'</td>'
        +'<td>'+sentimentCell(ipo)+'</td>'
        +'<td>'+sigCell(ipo)+'</td>'
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
    +'<th>Company</th><th>Status</th><th>Day</th><th>Closes</th>'
    +'<th>Listing</th><th>Price Band</th><th>Min Invest</th>'
    +'<th>Sub</th><th>QIB</th><th>Retail</th>'
    +'<th>Allotment %</th><th>GMP / Listing</th><th>Trend</th>'
    +'<th>Profit</th><th>ROI %</th><th>Sentiment</th><th>Rating /5 \\u26a0</th>'
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
    +'<div class="disclaimer">\\u26a0 <strong>Rating /5</strong> is a composite score combining 9 factors: QIB subscription (20%), promoter holding (15%), OFS% (15%), use of proceeds (10%), GMP% (10%), IG rating (10%), overall subscription (10%), allotment odds (5%), ROI% (5%). Hover the rating to see factor breakdown. SME IPOs get an 8% discount. Hard caps apply for red flags. Fundamentals data from Chittorgarh individual IPO pages.</div>';
}

function setStatus(s){
  statusFilter=s;
  document.querySelectorAll('#status-tabs .tab').forEach(function(b){b.classList.toggle('active',b.dataset.s===s);});
  renderContent();
}
function setSort(s){sortBy=s;renderContent();}
function setSearch(v){searchQuery=v;renderContent();}
function doRefresh(){
  var btn=document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  btn.disabled=true;
  // Show "Refreshing..." immediately so user sees feedback
  var ts=document.getElementById('refresh-ts');
  if(ts)ts.textContent='↻ Refreshing…';
  setTimeout(function(){
    window.location.href=window.location.href.split('?')[0]+'?_='+Date.now();
  },400);
}

function toIST(d){return new Date(d.toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));}
function fmtIST(d){
  var ist=toIST(d);
  return ist.toLocaleDateString('en-IN',{day:'2-digit',month:'short'})+' '+
         ist.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',hour12:true})+' IST';
}

(function init(){
  allIpos=loadIpos();

  // Data timestamp — when GitHub Actions last regenerated the HTML (baked in)
  var dataTime=fmtIST(new Date(snap.generatedAt));

  // Refreshed timestamp — when this browser tab loaded the page (always current)
  var refreshTime=fmtIST(new Date());

  document.getElementById('updated-label').innerHTML=
    '<span class="live-dot"></span>'
    +'<span style="display:flex;flex-direction:column;gap:1px;line-height:1.3">'
    +'<span style="color:var(--text-2)">Data: '+dataTime+'</span>'
    +'<span id="refresh-ts" style="color:var(--text-3);font-size:10px">\\u21bb Refreshed: '+refreshTime+'</span>'
    +'</span>';

  renderContent();
})();

</script>
</div><!-- /app -->
</body>
</html>"""


def save_html(ipos: list[IPO], path: str) -> None:
    import json
    import os
    from datetime import datetime, timezone

    import re as _re
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_json = json.dumps([ipo_to_dict(i) for i in ipos], ensure_ascii=False)

    abs_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    # If the file already exists, update only the IPO_SNAPSHOT in place so all
    # manual JS/HTML edits to the file are preserved. Fall back to _HTML_TEMPLATE
    # only when creating the file for the first time.
    if os.path.exists(abs_path):
        with open(abs_path, encoding="utf-8") as f:
            existing = f.read()
        new_snapshot = f'window.IPO_SNAPSHOT={{generatedAt:"{generated_at}",data:{data_json}}};'
        html = _re.sub(
            r'window\.IPO_SNAPSHOT=\{generatedAt:"[^"]*",data:\[.*?\]\};',
            new_snapshot,
            existing,
            count=1,
            flags=_re.DOTALL,
        )
    else:
        html = _HTML_TEMPLATE.replace("__GENERATED_AT__", generated_at).replace("__IPO_DATA__", data_json)

    with open(abs_path, "w", encoding="utf-8") as f:
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
        console.print("[red]Could not fetch IPO data — check connectivity / site may be blocking this IP.[/red]")
        if args.output_html:
            # In CI/dashboard mode keep the existing file unchanged so GitHub Pages
            # isn't wiped out by a transient network block on the runner IP.
            console.print("[yellow]Dashboard not updated; existing file retained.[/yellow]")
            sys.exit(0)
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
