/**
 * IPO Tracker — Cloudflare Worker
 *
 * Fetches all data from Chittorgarh + InvestorGain in parallel, merges it,
 * and returns a single JSON payload to the browser dashboard.
 *
 * Deploy:
 *   npx wrangler deploy
 *
 * Test locally:
 *   npx wrangler dev
 *   curl "http://localhost:8787?year=2026"
 */

const CG = "https://www.chittorgarh.com/";
const IG = "https://www.investorgain.com/";
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Content-Type": "application/json",
  "Cache-Control": "no-cache",
};

async function getRows(url, referer) {
  try {
    const r = await fetch(url + "?search=", {
      headers: { "User-Agent": UA, "Referer": referer },
    });
    if (!r.ok) return [];
    const j = await r.json();
    return j.reportTableData || [];
  } catch {
    return [];
  }
}

function norm(n) {
  return n.toLowerCase()
    .replace(/\b(ltd|limited|ipo|the)\b/g, "")
    .replace(/[^a-z0-9]+/g, "");
}

function minLots(category, priceHigh, lotSize) {
  if (priceHigh === null || lotSize === null || priceHigh * lotSize === 0) return 1;
  if (category === "SME") return Math.max(1, Math.ceil(100000 / (priceHigh * lotSize)));
  return 1;
}

function stripHtml(s) {
  return (s || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function fp(v) {
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

function subStr(x) {
  if (x === null) return "-";
  if (x < 1) return "Weak";
  if (x < 3) return "Average";
  if (x < 10) return "Strong";
  return "Exceptional";
}

function compositeScore(ipo) {
  const F = {};

  // Factor 1: QIB Sub (20%)
  const q = ipo.qibSub;
  F["QIB Sub"] = { score: q !== null ? (q >= 50 ? 1 : q >= 20 ? 0.8 : q >= 10 ? 0.6 : q >= 5 ? 0.4 : q >= 2 ? 0.2 : 0) : 0.3, weight: 0.20, note: q !== null ? `${q.toFixed(1)}x` : "No data" };

  // Factor 2: Overall Sub (10%)
  const ssMap = { Exceptional: 1.0, Strong: 0.75, Average: 0.4, Weak: 0.0 };
  const ss = subStr(ipo.overallSub);
  F["Overall Sub"] = { score: ssMap[ss] ?? 0.3, weight: 0.10, note: ipo.overallSub !== null ? `${ipo.overallSub.toFixed(1)}x` : "No data" };

  // Factor 3: Allotment Odds (5%)
  const a = ipo.allotmentOdds;
  F["Allotment Odds"] = { score: a !== null ? (a > 20 ? 1 : a > 10 ? 0.75 : a > 5 ? 0.5 : a > 2 ? 0.25 : 0) : 0.3, weight: 0.05, note: a !== null ? `${a.toFixed(1)}%` : "No data" };

  // Factor 4: ROI% (5%)
  const r = ipo.roiPct;
  F["ROI (GMP)"] = { score: r !== null ? (r >= 30 ? 1 : r >= 15 ? 0.75 : r >= 5 ? 0.5 : r >= 0 ? 0.25 : 0) : 0.3, weight: 0.05, note: r !== null ? `${r.toFixed(1)}%` : "No data" };

  // Factor 5: GMP% (10%)
  const g = ipo.gmpPct;
  F["GMP %"] = { score: g !== null ? (g >= 30 ? 1 : g >= 15 ? 0.75 : g >= 5 ? 0.5 : g >= 0 ? 0.25 : 0) : 0.3, weight: 0.10, note: g !== null ? `${g.toFixed(1)}%` : "No data" };

  // Factor 6: IG Rating (10%)
  const rat = ipo.rating;
  const ratMap = { 5: 1.0, 4: 0.75, 3: 0.5, 2: 0.25, 1: 0.0 };
  F["IG Rating"] = { score: rat !== null ? (ratMap[rat] ?? 0.3) : 0.3, weight: 0.10, note: rat !== null ? `${rat}/5` : "No data" };

  // Factors 7-9: per-IPO data not available in bulk APIs — use neutral defaults
  F["Promoter Hold"]    = { score: 0.4, weight: 0.15, note: "No data (live mode)" };
  F["OFS %"]            = { score: 0.5, weight: 0.15, note: "No data (live mode)" };
  F["Use of Proceeds"]  = { score: 0.4, weight: 0.10, note: "No data (live mode)" };

  let score = Math.round(Object.values(F).reduce((s, f) => s + f.score * f.weight, 0) * 5 * 10) / 10;
  let cappedBy = null;
  const cap = (max, reason) => { if (score > max) { score = max; cappedBy = reason; } };

  if (q !== null && q < 1) cap(2.0, "QIB <1x");
  if (g !== null && g < 0) cap(3.0, "Negative GMP");
  if (ipo.category === "SME") {
    score = Math.round(score * 0.92 * 10) / 10;
    if (!cappedBy) cappedBy = "SME discount applied";
  }
  score = Math.max(1.0, Math.min(5.0, score));

  const label =
    score >= 4.5 ? "Strong Buy" :
    score >= 3.5 ? "Buy" :
    score >= 2.5 ? "Research Needed" :
    score >= 1.5 ? "Caution" : "Avoid";

  for (const f of Object.values(F)) {
    f.contribution = Math.round(f.score * f.weight * 5 * 100) / 100;
  }
  return { score, label, factors: F, capped_by: cappedBy };
}

async function scrapeMinLots(slug, lotSize) {
  if (!slug || !lotSize) return null;
  try {
    const r = await fetch(`https://www.chittorgarh.com/ipo/${slug}/`, {
      headers: { "User-Agent": UA, "Referer": CG },
    });
    if (!r.ok) return null;
    const text = await r.text();
    const clean = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
    // "minimum amount... ₹X,XX,XXX (N,NNN shares)"
    const m = clean.match(/minimum amount[^₹]*₹[\d,]+\s*\(([\d,]+)\s*shares?\)/i);
    if (m) {
      const minShares = parseInt(m[1].replace(/,/g, ""));
      return Math.max(1, Math.ceil(minShares / lotSize));
    }
  } catch { /* ignore */ }
  return null;
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: { ...CORS, "Access-Control-Allow-Methods": "GET, OPTIONS" },
      });
    }

    const url = new URL(request.url);
    const year = parseInt(url.searchParams.get("year") || new Date().getFullYear());
    const fy = `${year}-${String(year + 1).slice(-2)}`;
    const cgBase = `https://webnodejs.chittorgarh.com/cloud/report/data-read`;
    const igBase = `https://webnodejs.investorgain.com/cloud/v2/report/data-read`;

    try {
      const [cgMain, igRows, cgSubMb, cgSubSme, cgListing] = await Promise.all([
        getRows(`${cgBase}/82/1/6/${year}/${fy}/0/all/0`, CG),
        getRows(`${igBase}/331/1/6/${year}/${fy}/0/all`, IG),
        getRows(`${cgBase}/21/1/6/${year}/${fy}/0/mainboard/0`, CG),
        getRows(`${cgBase}/21/1/6/${year}/${fy}/0/sme/0`, CG),
        getRows(`${cgBase}/98/1/6/${year}/${fy}/0/all/0`, CG),
      ]);

      // ---- InvestorGain lookup ----
      const igLookup = {};
      for (const row of igRows) {
        const name = (row["~ipo_name"] || "").trim();
        if (!name) continue;
        const gmpText = row["GMP"] || "";
        const gm = gmpText.match(/<b>(-?[\d.]+|--)<\/b>\s*\(([-\d.]+)%\)/);
        const gmp = gm && gm[1] !== "--" ? fp(gm[1]) : null;
        const gmpPct = gm && gmp !== null ? fp(gm[2]) : null;
        const rm = gmpText.match(/<b>(-?[\d.]+)\s*[↓↑↓↑]\s*\/\s*(-?[\d.]+)\s*[↓↑↓↑]<\/b>/);
        const subM = (row["Sub"] || "").match(/([\d.]+)x/);
        const ratingHtml = row["Rating"] || "";
        const rating = (ratingHtml.match(/&#128293;/g) || []).length || null;
        igLookup[norm(name)] = {
          gmp, gmpPct,
          gmpLow: rm ? fp(rm[1]) : gmp,
          gmpHigh: rm ? fp(rm[2]) : gmp,
          overallSub: subM ? fp(subM[1]) : null,
          lotSize: row["Lot"] ? parseInt(row["Lot"]) || null : null,
          rating,
          pe: row["~P/E"] && row["~P/E"] !== "--" ? fp(row["~P/E"]) : null,
          boaDate: (row["~Srt_BoA_Dt"] || "").substring(0, 10) || null,
          anchor: (row["Anchor"] || "").toLowerCase().includes("check"),
        };
      }

      // ---- CG subscription lookup ----
      const cgSub = {};
      for (const row of [...cgSubMb, ...cgSubSme]) {
        let n = stripHtml((row["Company"] || "").replace(/<span[^>]*>.*?<\/span>/gs, ""));
        n = n.replace(/\s+(Ltd\.?|Limited)\s*$/i, "").trim();
        if (!n) continue;
        cgSub[norm(n)] = {
          qibSub: fp(row["QIB (x)"]),
          niiSub: fp(row["NII (x)"]),
          retailSub: fp(row["Retail (x)"]),
          empSub: fp(row["Employee (x)"]),
          totalSub: fp(row["Total (x)"]),
          applications: row["Applications"] || null,
        };
      }

      // ---- CG listing lookup ----
      const cgListingMap = {};
      for (const row of cgListing) {
        let n = (row["Company"] || "").replace(/\s+(Ltd\.?|Limited)\.?\s*$/i, "").trim();
        if (!n) continue;
        const gainRaw = stripHtml(row["% Gain / Loss (Issue price v/s Close price on Listing)"] || "");
        const gainPct = gainRaw ? fp(gainRaw) : null;
        const openP = fp(row["Open Price on Listing (Rs.)"]);
        const closeP = fp(row["Close Price on Listing (Rs.)"]);
        if (openP !== null || gainPct !== null) {
          cgListingMap[norm(n)] = { listingOpenPrice: openP, listingClosePrice: closeP, listingGainPct: gainPct };
        }
      }

      // ---- Build IPO list ----
      const todayD = new Date();
      todayD.setUTCHours(0, 0, 0, 0);

      const ipos = [];
      for (const row of cgMain) {
        let rawName = row["~IPO"] || "";
        if (!rawName) rawName = stripHtml((row["Company"] || "").replace(/<span[^>]*>.*?<\/span>/gs, ""));
        const name = rawName
          .replace(/\s+IPO\s*$/i, "")
          .replace(/\s+(Ltd\.?|Limited)\.?\s*$/i, "")
          .trim();
        if (!name) continue;

        const catStr = (row["Issue Category"] || "").trim();
        const category = catStr === "IPO" || catStr === "Mainboard" ? "Mainboard" : "SME";

        const priceStr = String(row["Issue Price (Rs.)"] || "");
        const pmatch = priceStr.match(/([\d.]+)\s*(?:to|-)\s*([\d.]+)/);
        const pm2 = priceStr.match(/([\d.]+)/);
        const priceLow = pmatch ? fp(pmatch[1]) : (pm2 ? fp(pm2[1]) : null);
        const priceHigh = pmatch ? fp(pmatch[2]) : priceLow;

        const openDate = (row["~Issue_Open_Date"] || "").substring(0, 10) || null;
        const closeDate = (row["~IssueCloseDate"] || "").substring(0, 10) || null;
        const listingDate = (row["~ListingDate"] || "").substring(0, 10) || null;
        const issueSizeCr = fp(row["Total Issue Amount (Incl.Firm reservations) (Rs.cr.)"]);

        const od = openDate ? new Date(openDate + "T00:00:00Z") : null;
        const cd = closeDate ? new Date(closeDate + "T00:00:00Z") : null;
        let status = "Closed";
        if (od && todayD < od) status = "Upcoming";
        else if (od && cd && todayD >= od && todayD <= cd) status = "Open";

        const compHtml = row["Company"] || "";
        const idM = compHtml.match(/\/ipo\/[^/]+\/(\d+)\//);
        const cgSlug = row["~URLRewrite_Folder_Name"] && idM
          ? `${row["~URLRewrite_Folder_Name"]}/${idM[1]}`
          : (row["~URLRewrite_Folder_Name"] || "");

        const key = norm(name);
        const ig = igLookup[key] || {};
        const sub = cgSub[key] || {};
        const lst = cgListingMap[key] || {};

        const lotSize = ig.lotSize ?? null;
        const gmp = ig.gmp ?? null;
        const retailSub = sub.retailSub ?? null;
        const lots = minLots(category, priceHigh, lotSize);
        const expectedProfit = gmp !== null && lotSize !== null ? Math.round(gmp * lotSize * lots) : null;
        const minInv = priceHigh !== null && lotSize !== null ? Math.round(priceHigh * lotSize * lots) : null;
        const roiPct = expectedProfit !== null && minInv ? Math.round(expectedProfit / minInv * 1000) / 10 : null;
        const allotmentOdds = retailSub !== null && retailSub > 0 ? Math.round(Math.min(1 / retailSub * 100, 100) * 10) / 10 : null;

        const now = todayD.getTime();
        const daysToClose = cd ? Math.round((cd.getTime() - now) / 86400000) : null;
        const ld = listingDate ? new Date(listingDate + "T00:00:00Z") : null;
        const daysToListing = ld ? Math.round((ld.getTime() - now) / 86400000) : null;

        const ipo = {
          name, category, status,
          openDate, closeDate, listingDate,
          boaDate: ig.boaDate || null,
          priceLow, priceHigh,
          lotSize, minLots: lots, issueSizeCr,
          pe: ig.pe ?? null,
          gmp, gmpPct: ig.gmpPct ?? null,
          gmpLow: ig.gmpLow ?? null, gmpHigh: ig.gmpHigh ?? null,
          rating: ig.rating ?? null,
          anchor: ig.anchor || false,
          overallSub: ig.overallSub ?? sub.totalSub ?? null,
          qibSub: sub.qibSub ?? null,
          niiSub: sub.niiSub ?? null,
          retailSub, empSub: sub.empSub ?? null,
          applications: sub.applications || null,
          expectedProfit, roiPct, allotmentOdds,
          daysToClose: daysToClose !== null && daysToClose >= 0 ? daysToClose : null,
          daysToListing: daysToListing !== null && daysToListing >= 0 ? daysToListing : null,
          listingOpenPrice: lst.listingOpenPrice ?? null,
          listingClosePrice: lst.listingClosePrice ?? null,
          listingGainPct: lst.listingGainPct ?? null,
          brokerApply: 0, brokerAvoid: 0, brokerNeutral: 0,
          memberApply: 0, memberAvoid: 0, memberNeutral: 0,
          promoterPre: null, promoterPost: null,
          freshPct: null, ofsPct: null,
          proceedsCapex: 0, proceedsDebt: 0, proceedsWc: 0, proceedsGeneral: 0,
          iwReview: null,
          sources: ["chittorgarh", ...(ig.gmp !== undefined ? ["investorgain"] : [])],
          cgSlug,
        };

        const cs = compositeScore(ipo);
        ipo.compositeScore = cs.score;
        ipo.compositeLabel = cs.label;
        ipo.compositeFactors = Object.fromEntries(
          Object.entries(cs.factors).map(([k, v]) => [k, { score: v.score, weight: v.weight, contribution: v.contribution, note: v.note }])
        );
        ipo.compositeCappedBy = cs.capped_by;

        ipos.push(ipo);
      }

      // Scrape actual min lots from CG pages for Open/Upcoming IPOs in parallel
      await Promise.all(
        ipos
          .filter(i => (i.status === "Open" || i.status === "Upcoming") && i.cgSlug && i.lotSize)
          .map(async i => {
            const scraped = await scrapeMinLots(i.cgSlug, i.lotSize);
            if (scraped !== null && scraped !== i.minLots) {
              i.minLots = scraped;
              i.expectedProfit = i.gmp !== null ? Math.round(i.gmp * i.lotSize * scraped) : null;
              const mi = i.priceHigh !== null ? Math.round(i.priceHigh * i.lotSize * scraped) : null;
              i.roiPct = i.expectedProfit !== null && mi ? Math.round(i.expectedProfit / mi * 1000) / 10 : null;
            }
          })
      );

      return new Response(
        JSON.stringify({ ipos, generatedAt: new Date().toISOString() }),
        { headers: CORS }
      );
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: CORS });
    }
  },
};
