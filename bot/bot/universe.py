"""Symbol metadata for the universe — name, sector, IBKR contract bits.

Each entry: (name, sector, currency, primary_exchange[, asset_class]). Used by
broker.py to build fully-qualified IB contracts and by the dashboard for display.

asset_class = "stock" (default) builds an IB Stock(symbol, "SMART", currency,
primaryExchange=...). asset_class = "crypto" builds an IB Crypto(symbol, "PAXOS",
"USD") — IBKR only supports crypto on PAXOS in USD; primary_exchange is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Meta:
    name: str
    sector: str
    currency: str
    primary_exchange: str  # IBKR primaryExchange; "" for US SMART-resolved
    asset_class: str = "stock"  # "stock" or "crypto"


UNIVERSE_META: dict[str, Meta] = {
    # --- US large-caps (SMART routing, USD) ---
    "AAPL":  Meta("Apple",             "Tech",       "USD", ""),
    "MSFT":  Meta("Microsoft",         "Tech",       "USD", ""),
    "GOOGL": Meta("Alphabet",          "Tech",       "USD", ""),
    "AMZN":  Meta("Amazon",            "Consumer",   "USD", ""),
    "META":  Meta("Meta Platforms",    "Tech",       "USD", ""),
    "NVDA":  Meta("NVIDIA",            "Tech",       "USD", ""),
    "TSLA":  Meta("Tesla",             "Consumer",   "USD", ""),
    "AVGO":  Meta("Broadcom",          "Tech",       "USD", ""),
    "JPM":   Meta("JPMorgan Chase",    "Finance",    "USD", ""),
    "V":     Meta("Visa",              "Finance",    "USD", ""),
    "MA":    Meta("Mastercard",        "Finance",    "USD", ""),
    "UNH":   Meta("UnitedHealth",      "Healthcare", "USD", ""),
    "HD":    Meta("Home Depot",        "Consumer",   "USD", ""),
    "PG":    Meta("Procter & Gamble",  "Consumer",   "USD", ""),
    "JNJ":   Meta("Johnson & Johnson", "Healthcare", "USD", ""),
    "XOM":   Meta("Exxon Mobil",       "Energy",     "USD", ""),
    "CVX":   Meta("Chevron",           "Energy",     "USD", ""),
    "KO":    Meta("Coca-Cola",         "Consumer",   "USD", ""),
    "PEP":   Meta("PepsiCo",           "Consumer",   "USD", ""),
    "WMT":   Meta("Walmart",           "Consumer",   "USD", ""),
    "COST":  Meta("Costco",            "Consumer",   "USD", ""),
    "MCD":   Meta("McDonald's",        "Consumer",   "USD", ""),
    "DIS":   Meta("Disney",            "Consumer",   "USD", ""),
    "NFLX":  Meta("Netflix",           "Tech",       "USD", ""),
    "CRM":   Meta("Salesforce",        "Tech",       "USD", ""),
    "ORCL":  Meta("Oracle",            "Tech",       "USD", ""),
    "ADBE":  Meta("Adobe",             "Tech",       "USD", ""),
    "INTC":  Meta("Intel",             "Tech",       "USD", ""),
    "AMD":   Meta("AMD",               "Tech",       "USD", ""),
    "QCOM":  Meta("Qualcomm",          "Tech",       "USD", ""),

    # --- EU blue-chips (SMART + primaryExchange; local currency) ---
    # Symbols below use IBKR local tickers. Keep the key lowercase where IBKR
    # uses multi-character tickers to avoid conflicts with US symbols of the
    # same root (e.g. Shell US "SHEL" vs LSE "SHEL").
    "ASML":  Meta("ASML Holding",         "Tech",       "EUR", "AEB"),   # Euronext Amsterdam
    "MC":    Meta("LVMH",                 "Consumer",   "EUR", "SBF"),   # Euronext Paris
    "OR":    Meta("L'Oreal",              "Consumer",   "EUR", "SBF"),
    "AIR":   Meta("Airbus",               "Tech",       "EUR", "SBF"),
    "TTE":   Meta("TotalEnergies",        "Energy",     "EUR", "SBF"),
    "RMS":   Meta("Hermes",               "Consumer",   "EUR", "SBF"),
    "SAP":   Meta("SAP",                  "Tech",       "EUR", "IBIS"),  # XETRA
    "SIE":   Meta("Siemens",              "Tech",       "EUR", "IBIS"),
    "ALV":   Meta("Allianz",              "Finance",    "EUR", "IBIS"),
    "DTE":   Meta("Deutsche Telekom",     "Tech",       "EUR", "IBIS"),
    "BAS":   Meta("BASF",                 "Energy",     "EUR", "IBIS"),
    "AZN":   Meta("AstraZeneca",          "Healthcare", "GBP", "LSE"),
    "SHEL":  Meta("Shell",                "Energy",     "GBP", "LSE"),
    "HSBA":  Meta("HSBC",                 "Finance",    "GBP", "LSE"),
    "ULVR":  Meta("Unilever",             "Consumer",   "GBP", "LSE"),
    "NESN":  Meta("Nestle",               "Consumer",   "CHF", "EBS"),   # SIX Swiss
    "NOVN":  Meta("Novartis",             "Healthcare", "CHF", "EBS"),
    # US-listed ADRs replace direct listings that IBKR paper cannot qualify
    # (NOVO-B CPH, ROG EBS, SAN SBF returned Error 200 every scan tick).
    # ADRs trade in USD with SMART routing, dodging the paper-data gap.
    "NVO":   Meta("Novo Nordisk ADR",     "Healthcare", "USD", ""),
    "SNY":   Meta("Sanofi ADR",           "Healthcare", "USD", ""),

    # --- Crypto (IBKR PAXOS, USD, 24/7) ---
    # IBKR supports BTC/ETH/LTC/BCH on PAXOS only. Orders must be LMT (no MKT),
    # outsideRth=True required; commission ~0.18% with $1.75 floor.
    "BTC":   Meta("Bitcoin",  "Crypto", "USD", "PAXOS", asset_class="crypto"),
    "ETH":   Meta("Ethereum", "Crypto", "USD", "PAXOS", asset_class="crypto"),
    "LTC":   Meta("Litecoin", "Crypto", "USD", "PAXOS", asset_class="crypto"),
    "BCH":   Meta("Bitcoin Cash", "Crypto", "USD", "PAXOS", asset_class="crypto"),
}


def meta(symbol: str) -> Meta:
    return UNIVERSE_META.get(symbol, Meta(symbol, "Other", "USD", ""))


def is_crypto(symbol: str) -> bool:
    return meta(symbol).asset_class == "crypto"
