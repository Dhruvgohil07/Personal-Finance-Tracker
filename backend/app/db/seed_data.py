"""The data that `app/db/seed.py` loads: system categories and common merchants.

Plain Python data, no database code, so it can be unit tested on its own.
To change a name, icon, colour or kind, edit it here and re-run the seed:
the seed *upserts* (insert, or update if it already exists), matching rows
by slug / normalized_key.
"""

from dataclasses import dataclass

from app.models.category import CategoryKind


@dataclass(frozen=True)
class SeedCategory:
    slug: str
    name: str
    kind: CategoryKind
    icon: str
    color: str


@dataclass(frozen=True)
class SeedMerchant:
    normalized_key: str
    display_name: str
    default_category_slug: str


# SPEC §5 system categories. Icons are lucide icon names (frontend, Phase 3).
SYSTEM_CATEGORIES: tuple[SeedCategory, ...] = (
    # --- Expenses ---
    SeedCategory("food-dining", "Food & Dining", CategoryKind.EXPENSE, "utensils", "#F97316"),
    SeedCategory("groceries", "Groceries", CategoryKind.EXPENSE, "shopping-basket", "#22C55E"),
    SeedCategory("shopping", "Shopping", CategoryKind.EXPENSE, "shopping-bag", "#EC4899"),
    SeedCategory("transport", "Transport", CategoryKind.EXPENSE, "car", "#3B82F6"),
    SeedCategory("fuel", "Fuel", CategoryKind.EXPENSE, "fuel", "#EAB308"),
    SeedCategory("travel", "Travel", CategoryKind.EXPENSE, "plane", "#06B6D4"),
    SeedCategory(
        "bills-utilities", "Bills & Utilities", CategoryKind.EXPENSE, "receipt", "#64748B"
    ),
    SeedCategory(
        "mobile-internet", "Mobile & Internet", CategoryKind.EXPENSE, "smartphone", "#8B5CF6"
    ),
    SeedCategory("rent", "Rent", CategoryKind.EXPENSE, "house", "#A16207"),
    SeedCategory("entertainment", "Entertainment", CategoryKind.EXPENSE, "clapperboard", "#E11D48"),
    SeedCategory("subscriptions", "Subscriptions", CategoryKind.EXPENSE, "repeat", "#7C3AED"),
    SeedCategory("health", "Health", CategoryKind.EXPENSE, "heart-pulse", "#EF4444"),
    SeedCategory("education", "Education", CategoryKind.EXPENSE, "graduation-cap", "#0EA5E9"),
    SeedCategory("emi-loans", "EMI & Loans", CategoryKind.EXPENSE, "landmark", "#B45309"),
    SeedCategory("investments", "Investments", CategoryKind.EXPENSE, "trending-up", "#16A34A"),
    SeedCategory("insurance", "Insurance", CategoryKind.EXPENSE, "shield", "#0891B2"),
    SeedCategory(
        "fees-charges", "Fees & Charges", CategoryKind.EXPENSE, "badge-percent", "#DC2626"
    ),
    SeedCategory("cash-withdrawal", "Cash Withdrawal", CategoryKind.EXPENSE, "banknote", "#65A30D"),
    SeedCategory(
        "transfers-to-people", "Transfers to People", CategoryKind.EXPENSE, "users", "#F59E0B"
    ),
    # --- Transfers (excluded from spending and income totals) ---
    SeedCategory(
        "self-transfer", "Self Transfer", CategoryKind.TRANSFER, "arrow-left-right", "#94A3B8"
    ),
    # --- Income ---
    SeedCategory("salary", "Salary", CategoryKind.INCOME, "briefcase", "#15803D"),
    SeedCategory("refunds", "Refunds", CategoryKind.INCOME, "undo-2", "#0D9488"),
    SeedCategory("interest", "Interest", CategoryKind.INCOME, "percent", "#059669"),
    SeedCategory("other-income", "Other Income", CategoryKind.INCOME, "circle-plus", "#4ADE80"),
    # --- Fallback when nothing else matches (SPEC §8, step 7) ---
    SeedCategory("uncategorized", "Uncategorized", CategoryKind.EXPENSE, "circle-help", "#9CA3AF"),
)


# Common Indian merchants (SPEC §5). Keys are uppercase with no spaces or
# punctuation; the exact key format will be confirmed when normalization
# (SPEC §6.5) is built from real statement samples in Phase 2.
COMMON_MERCHANTS: tuple[SeedMerchant, ...] = (
    # Food delivery / restaurants
    SeedMerchant("ZOMATO", "Zomato", "food-dining"),
    SeedMerchant("SWIGGY", "Swiggy", "food-dining"),
    SeedMerchant("DOMINOS", "Domino's", "food-dining"),
    SeedMerchant("MCDONALDS", "McDonald's", "food-dining"),
    SeedMerchant("STARBUCKS", "Starbucks", "food-dining"),
    # Groceries / quick commerce
    SeedMerchant("BLINKIT", "Blinkit", "groceries"),
    SeedMerchant("ZEPTO", "Zepto", "groceries"),
    SeedMerchant("BIGBASKET", "BigBasket", "groceries"),
    SeedMerchant("DMART", "DMart", "groceries"),
    # Shopping
    SeedMerchant("AMAZON", "Amazon", "shopping"),
    SeedMerchant("FLIPKART", "Flipkart", "shopping"),
    SeedMerchant("MYNTRA", "Myntra", "shopping"),
    SeedMerchant("AJIO", "AJIO", "shopping"),
    SeedMerchant("NYKAA", "Nykaa", "shopping"),
    # Transport
    SeedMerchant("UBER", "Uber", "transport"),
    SeedMerchant("OLA", "Ola", "transport"),
    SeedMerchant("RAPIDO", "Rapido", "transport"),
    # Travel
    SeedMerchant("IRCTC", "IRCTC", "travel"),
    SeedMerchant("MAKEMYTRIP", "MakeMyTrip", "travel"),
    SeedMerchant("INDIGO", "IndiGo", "travel"),
    # Subscriptions / entertainment
    SeedMerchant("NETFLIX", "Netflix", "subscriptions"),
    SeedMerchant("SPOTIFY", "Spotify", "subscriptions"),
    SeedMerchant("JIOHOTSTAR", "JioHotstar", "subscriptions"),
    SeedMerchant("BOOKMYSHOW", "BookMyShow", "entertainment"),
    # Mobile & internet
    SeedMerchant("JIO", "Jio", "mobile-internet"),
    SeedMerchant("AIRTEL", "Airtel", "mobile-internet"),
    # Health
    SeedMerchant("PHARMEASY", "PharmEasy", "health"),
    SeedMerchant("NETMEDS", "Netmeds", "health"),
    # Others
    SeedMerchant("NESCAFE", "Nescafe", "food-dining"),
    SeedMerchant("AMUL", "Amul", "groceries"),
    SeedMerchant("INDIANOIL", "Indian Oil", "fuel"),
)
