#!/usr/bin/env python3
"""
Haalt dagkoersen op bij Yahoo Finance en schrijft koersen.json.

Alles wordt omgerekend naar euro, zodat de portefeuillepagina er direct mee
kan rekenen. Gebruikt alleen de standaardbibliotheek van Python: geen
installaties, dus niets dat stuk kan gaan door een pakketupdate.

Welke fondsen opgehaald worden staat in fondsen.json.
"""

import datetime
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "fondsen.json")
OUTPUT = os.path.join(HERE, "koersen.json")

# Yahoo weigert verzoeken zonder browserachtige kop.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(*a):
    print(*a, flush=True)


def fetch_json(url, tries=4):
    """Haalt JSON op, met een paar nieuwe pogingen bij tijdelijke fouten."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # netwerk, 429, tijdelijke 5xx
            last = exc
            if attempt < tries - 1:
                wait = 3 * (attempt + 1)
                log("   poging %d mislukt (%s), wacht %ds" % (attempt + 1, exc, wait))
                time.sleep(wait)
    raise last


def daily_series(symbol, start_date):
    """Geeft {datum: slotkoers} plus de valuta waarin het fonds noteert."""
    p1 = int(datetime.datetime.strptime(start_date, "%Y-%m-%d")
             .replace(tzinfo=datetime.timezone.utc).timestamp())
    p2 = int(time.time()) + 86400
    query = urllib.parse.urlencode({
        "period1": p1, "period2": p2, "interval": "1d",
        "includePrePost": "false", "events": "div,splits",
    })
    err = None
    for host in ("query1", "query2"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (
            host, urllib.parse.quote(symbol, safe="=^.-"), query)
        try:
            data = fetch_json(url)
        except Exception as exc:
            err = exc
            continue
        chart = (data or {}).get("chart") or {}
        if chart.get("error"):
            raise RuntimeError("Yahoo kent symbool %r niet: %s" % (
                symbol, chart["error"].get("description", chart["error"])))
        results = chart.get("result") or []
        if not results:
            err = RuntimeError("leeg antwoord voor %r" % symbol)
            continue
        res = results[0]
        meta = res.get("meta") or {}
        stamps = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        raw = meta.get("currency") or "EUR"
        minor = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
                 "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}
        if raw in minor:
            currency, divisor = minor[raw]
        else:
            currency, divisor = raw.upper(), 1.0
        series = {}
        for ts, close in zip(stamps, closes):
            if close is None:
                continue
            day = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%Y-%m-%d")
            series[day] = round(float(close) / divisor, 6)
        if not series:
            err = RuntimeError("geen koersen in antwoord voor %r" % symbol)
            continue
        return series, currency, meta, divisor
    raise err


# ---------------------------------------------------------------------------
# Optioneel: sectorweging en de grootste posities per ETF.
#
# Yahoo verlangt hiervoor een cookie plus een "crumb", en verandert daar
# regelmatig iets aan. Lukt het niet, dan slaan we het over: je koersen
# hebben er niets van te lijden. In het logboek staat wat er gebeurde.
# ---------------------------------------------------------------------------
_CRUMB = {"value": None, "opener": None}


def crumb_opener():
    """Cookie ophalen bij Yahoo en daarmee een crumb bemachtigen."""
    if _CRUMB["value"] is not None:
        return _CRUMB["opener"], _CRUMB["value"]
    _CRUMB["value"] = ""          # één poging per run
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        req = urllib.request.Request("https://fc.yahoo.com", headers=HEADERS)
        try:
            opener.open(req, timeout=20).read(1)
        except urllib.error.HTTPError:
            pass                  # 404 is prima, de cookie is wat we willen
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v1/test/getcrumb", headers=HEADERS)
        crumb = opener.open(req, timeout=20).read().decode("utf-8").strip()
        if crumb and len(crumb) < 40:
            _CRUMB["value"], _CRUMB["opener"] = crumb, opener
            log("   profieldata: crumb verkregen")
        else:
            log("   profieldata: geen bruikbare crumb, sectoren worden overgeslagen")
    except Exception as exc:
        log("   profieldata: crumb ophalen mislukt (%s)" % exc)
    return _CRUMB["opener"], _CRUMB["value"]


def profile(symbol):
    """Geeft (sectorweging, holdings) of (None, None) als Yahoo niet meewerkt."""
    opener, crumb = crumb_opener()
    if not crumb or opener is None:
        return None, None
    url = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s"
           "?modules=topHoldings,assetProfile&crumb=%s"
           % (urllib.parse.quote(symbol, safe="=^.-"),
              urllib.parse.quote(crumb)))
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        data = json.loads(opener.open(req, timeout=30).read().decode("utf-8"))
    except Exception as exc:
        log("   profieldata overgeslagen (%s)" % exc)
        return None, None
    res = ((data.get("quoteSummary") or {}).get("result") or [{}])[0]
    top = res.get("topHoldings") or {}

    sectors = {}
    for item in top.get("sectorWeightings") or []:
        for key, val in item.items():
            raw = val.get("raw") if isinstance(val, dict) else val
            if raw:
                sectors[SECTOR_NL.get(key, key)] = round(float(raw), 6)

    holdings = []
    for h in top.get("holdings") or []:
        pct = h.get("holdingPercent")
        pct = pct.get("raw") if isinstance(pct, dict) else pct
        if not pct:
            continue
        holdings.append({"symbool": h.get("symbol") or "",
                         "naam": h.get("holdingName") or h.get("symbol") or "",
                         "gewicht": round(float(pct), 6)})

    if not sectors and not holdings:
        prof = res.get("assetProfile") or {}
        if prof.get("sector"):
            sectors = {SECTOR_NL.get(prof["sector"], prof["sector"]): 1.0}
    return (sectors or None), (holdings or None)


SECTOR_NL = {
    "realestate": "Vastgoed", "consumer_cyclical": "Luxe consumentengoederen",
    "basic_materials": "Basismaterialen", "consumer_defensive": "Basisconsumentengoederen",
    "technology": "Technologie", "communication_services": "Communicatiediensten",
    "financial_services": "Financiële diensten", "utilities": "Nutsbedrijven",
    "industrials": "Industrie", "energy": "Energie", "healthcare": "Gezondheidszorg",
    "Technology": "Technologie", "Financial Services": "Financiële diensten",
    "Healthcare": "Gezondheidszorg", "Industrials": "Industrie", "Energy": "Energie",
    "Utilities": "Nutsbedrijven", "Real Estate": "Vastgoed",
    "Consumer Cyclical": "Luxe consumentengoederen",
    "Consumer Defensive": "Basisconsumentengoederen",
    "Communication Services": "Communicatiediensten",
    "Basic Materials": "Basismaterialen",
}


def fx_series(currency, start_date):
    """Wisselkoers van `currency` naar EUR per dag."""
    if currency == "EUR":
        return {}
    series, cur, _meta, _div = daily_series("%sEUR=X" % currency, start_date)
    if cur not in ("EUR", currency):
        log("   let op: wisselkoers %sEUR=X noteert in %s" % (currency, cur))
    return series


def convert(series, fx):
    """Zet een koersreeks om naar euro met de wisselkoers van die dag."""
    if not fx:
        return dict(series)
    fx_days = sorted(fx)
    out, i, rate = {}, 0, None
    for day in sorted(series):
        while i < len(fx_days) and fx_days[i] <= day:
            rate = fx[fx_days[i]]
            i += 1
        if rate is None:
            rate = fx[fx_days[0]]
        out[day] = round(series[day] * rate, 6)
    return out


def main():
    if not os.path.exists(CONFIG):
        log("fondsen.json ontbreekt naast dit script.")
        return 1
    with open(CONFIG, encoding="utf-8") as fh:
        config = json.load(fh)

    start = config.get("vanaf", "2020-01-01")
    funds = config.get("fondsen") or []
    if not funds:
        log("Geen fondsen in fondsen.json.")
        return 1

    # Bestaande koersen behouden: als één fonds vandaag mislukt,
    # blijft de historie van de vorige keer staan.
    old = {"fondsen": {}}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT, encoding="utf-8") as fh:
                old = json.load(fh)
        except Exception:
            log("Bestaande koersen.json onleesbaar, begin opnieuw.")

    result = {}
    fx_cache = {}
    failed = []

    for fund in funds:
        isin = (fund.get("isin") or "").strip().upper()
        symbol = (fund.get("ticker") or "").strip()
        name = fund.get("naam") or isin
        if not isin or not symbol:
            continue
        log("%s  %s (%s)" % (isin, name, symbol))
        try:
            series, currency, meta, divisor = daily_series(symbol, start)
            # Tijdens beurstijd levert Yahoo de koers van dít moment mee.
            # Die zetten we op de dag van vandaag, zodat de portefeuille
            # niet op de slotkoers van gisteren blijft staan.
            live_stamp = None
            price_now = meta.get("regularMarketPrice")
            time_now = meta.get("regularMarketTime")
            if price_now and time_now:
                day_now = datetime.datetime.fromtimestamp(
                    int(time_now), datetime.timezone.utc)
                series[day_now.strftime("%Y-%m-%d")] = round(
                    float(price_now) / divisor, 6)
                live_stamp = day_now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if currency not in fx_cache:
                fx_cache[currency] = fx_series(currency, start)
            eur = convert(series, fx_cache[currency])
            last_day = max(eur)
            log("   %d dagen, laatste %s = EUR %.4f (bron: %s%s)" % (
                len(eur), last_day, eur[last_day], currency,
                ", live" if live_stamp else ""))
            merged = dict((old.get("fondsen", {}).get(isin) or {}).get("koersen") or {})
            merged.update(eur)
            entry = {"ticker": symbol, "naam": name,
                     "brutovaluta": currency, "koersen": merged}
            # Sector en land staan niet in de DeGiro-export; als je ze in
            # fondsen.json invult, geeft de robot ze door aan de pagina.
            for extra in ("sector", "land"):
                if fund.get(extra):
                    entry[extra] = fund[extra]
            if config.get("profieldata", True):
                sectors, holdings = profile(symbol)
                if sectors:
                    entry["sectorweging"] = sectors
                    log("   sectorweging: %d sectoren" % len(sectors))
                if holdings:
                    entry["holdings"] = holdings
                    log("   holdings: %d posities, grootste %s (%.1f%%)" % (
                        len(holdings), holdings[0]["naam"],
                        holdings[0]["gewicht"] * 100))
            else:
                oude = (old.get("fondsen", {}).get(isin) or {})
                for k in ("sectorweging", "holdings"):
                    if oude.get(k):
                        entry[k] = oude[k]
            if live_stamp:
                entry["koers_tijd"] = live_stamp
            result[isin] = entry
        except Exception as exc:
            log("   MISLUKT: %s" % exc)
            failed.append("%s (%s)" % (name, symbol))
            keep = old.get("fondsen", {}).get(isin)
            if keep:
                log("   koersen van de vorige keer blijven staan")
                result[isin] = keep

    if not result:
        log("Niets opgehaald en niets bewaard: koersen.json blijft ongewijzigd.")
        return 1

    # Bij een run elk half uur zou een nieuwe tijdstempel alleen al een
    # commit uitlokken. Daarom alleen wegschrijven als de koersen echt
    # veranderd zijn.
    def prices_only(funds):
        return {k: {"koersen": v.get("koersen"), "ticker": v.get("ticker"),
                    "sector": v.get("sector"), "land": v.get("land"),
                    "sectorweging": v.get("sectorweging"),
                    "holdings": v.get("holdings")}
                for k, v in (funds or {}).items()}

    if prices_only(old.get("fondsen")) == prices_only(result):
        log("Koersen ongewijzigd; koersen.json blijft zoals hij was.")
        return 0

    payload = {
        "bijgewerkt": datetime.datetime.now(datetime.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valuta": "EUR",
        "bron": "Yahoo Finance",
        "fondsen": result,
    }
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"))
    log("koersen.json geschreven: %d fondsen, %d koersen in totaal." % (
        len(result), sum(len(f["koersen"]) for f in result.values())))
    if failed:
        log("Niet opgehaald: %s" % ", ".join(failed))
        log("Controleer het symbool op finance.yahoo.com en pas fondsen.json aan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
