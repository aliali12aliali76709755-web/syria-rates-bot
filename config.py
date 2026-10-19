"""Currency configuration and data-source mapping for the Syrian exchange-rate bot."""

LIRASCOPE_BASE = "https://lirascope.syria-cloud.sy/api/v1"
LIRANEWS_BASE = "https://liranews.info/api/public/v1"

# Ordered list of fiat currencies quoted against SYP.
# lirascope: currency code inside /rates/latest (marketRates / cbsRates)
# liranews : symbol used in /price/<symbol>  (None when the source lacks it)
CURRENCIES = {
    "USD": {"name_ar": "دولار أمريكي", "flag": "🇺🇸", "lirascope": "USD", "liranews": "usdsypd"},
    "EUR": {"name_ar": "يورو", "flag": "🇪🇺", "lirascope": "EUR", "liranews": "eursyp"},
    "SAR": {"name_ar": "ريال سعودي", "flag": "🇸🇦", "lirascope": "SAR", "liranews": "sarsyp"},
    "TRY": {"name_ar": "ليرة تركية", "flag": "🇹🇷", "lirascope": "TRY", "liranews": None},
    "AED": {"name_ar": "درهم إماراتي", "flag": "🇦🇪", "lirascope": "AED", "liranews": "aedsyp"},
    "IQD": {"name_ar": "دينار عراقي", "flag": "🇮🇶", "lirascope": "IQD", "liranews": "iqdsyp"},
    "JOD": {"name_ar": "دينار أردني", "flag": "🇯🇴", "lirascope": "JOD", "liranews": "jodsyp"},
    "LBP": {"name_ar": "ليرة لبنانية", "flag": "🇱🇧", "lirascope": None, "liranews": "lbpsyp"},
}

# Gold (price per gram in SYP) — only LiraNews exposes these.
GOLD = {
    "G24": {"name_ar": "ذهب عيار 24", "liranews": "g24sypd"},
    "G21": {"name_ar": "ذهب عيار 21", "liranews": "g21sypd"},
    "G18": {"name_ar": "ذهب عيار 18", "liranews": "g18sypd"},
}

CURRENCY_ORDER = list(CURRENCIES.keys())
GOLD_ORDER = list(GOLD.keys())
