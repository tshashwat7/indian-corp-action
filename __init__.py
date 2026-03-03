"""
india_corp_actions
==================
Fetch Indian stock market corporate actions (dividends, splits, bonus issues,
rights, AGMs, buybacks, mergers) from NSE and BSE India.

100% free — no API keys or paid services required.

Quick start:
    from india_corp_actions import IndiaCorpActions

    client = IndiaCorpActions()
    df = client.get_actions_df()           # All upcoming actions (NSE)
    df = client.get_dividends()            # Only dividends
    df = client.get_actions_df(symbol="RELIANCE")  # Specific stock
"""

from .client import IndiaCorpActions
from .models import CorporateAction, classify_action

__all__ = ["IndiaCorpActions", "CorporateAction", "classify_action"]
__version__ = "0.1.0"
