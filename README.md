# india-corp-actions

Fetch Indian stock market **corporate actions** (dividends, splits, bonus issues, rights, AGMs, buybacks, mergers) directly from **NSE** and **BSE India**.

🆓 **100% free — no API keys, no paid services required.**

---

## Install

```bash
pip install requests pandas beautifulsoup4
# (until published to PyPI, install locally:)
pip install -e .
```

---

## Quick Start

```python
from india_corp_actions import IndiaCorpActions

client = IndiaCorpActions()

# All upcoming corporate actions (NSE, next 3 months)
df = client.get_actions_df()
print(df)

# Filter by stock symbol
df = client.get_actions_df(symbol="RELIANCE")

# Get only dividends
df = client.get_dividends()

# Get splits, bonus, rights, buybacks
df = client.get_splits()
df = client.get_bonus()
df = client.get_rights()
df = client.get_buybacks()

# Custom date range (DD-MM-YYYY for NSE)
df = client.get_actions_df(from_date="01-01-2025", to_date="31-03-2025")

# Use BSE as source
df = client.get_actions_df(source="BSE")

# Combine NSE + BSE
df = client.get_actions_df(source="both")
```

---

## All Methods

| Method | Description |
|---|---|
| `get_actions_df(...)` | All corporate actions → DataFrame |
| `get_actions(...)` | Same but returns `List[CorporateAction]` |
| `get_dividends()` | Upcoming dividends |
| `get_bonus()` | Upcoming bonus issues |
| `get_splits()` | Upcoming stock splits |
| `get_rights()` | Upcoming rights issues |
| `get_buybacks()` | Upcoming buybacks |
| `get_upcoming_results()` | Quarterly result dates (NSE) |
| `get_board_meetings()` | Board meeting schedule (NSE) |
| `get_announcements(symbol)` | Latest announcements (NSE) |

---

## Parameters

```python
client.get_actions_df(
    symbol="TCS",           # Optional: filter by NSE symbol
    from_date="01-01-2025", # DD-MM-YYYY — defaults to today
    to_date="31-03-2025",   # DD-MM-YYYY — defaults to 3 months ahead
    action_type="dividend", # Optional: dividend/bonus/split/rights/agm/buyback/merger/demerger/other
    source="NSE",           # "NSE" (default), "BSE", or "both"
)
```

---

## DataFrame Columns

| Column | Description |
|---|---|
| `symbol` | Stock symbol |
| `company_name` | Full company name |
| `action_type` | Classified type (dividend, bonus, split, ...) |
| `ex_date` | Ex-date ⭐ (most important for trading) |
| `record_date` | Record date |
| `details` | Raw description from exchange |
| `series` | EQ / BE / etc. |
| `face_value` | Face value of share |
| `bc_start_date` | Book closure start |
| `bc_end_date` | Book closure end |
| `source` | NSE or BSE |

---

## How it works

- **NSE**: Wraps the official `nseindia.com` JSON API endpoints. A session is established by warming cookies via the homepage first (required by NSE's anti-bot setup).
- **BSE**: Wraps the `api.bseindia.com` endpoints.
- No scraping of HTML, no paid APIs, no rate-limited public APIs.

---

## Run Tests

```bash
pip install pytest
pytest tests/
```

---

## License

MIT
