"""
india_corp_actions - Main Client
Unified interface for fetching Indian stock market corporate actions
from NSE and BSE India. 100% free, no API keys required.
"""

import logging
import pandas as pd
from typing import List, Optional, Literal

from .nse import NSEFetcher
from .bse import BSEFetcher
from .models import CorporateAction

logger = logging.getLogger(__name__)


class IndiaCorpActions:
    """
    Unified client to fetch corporate actions from NSE and BSE.

    Usage:
        client = IndiaCorpActions()

        # Get all upcoming corporate actions (NSE, next 3 months)
        df = client.get_actions_df()

        # Filter by stock
        df = client.get_actions_df(symbol="RELIANCE")

        # Filter by type: 'dividend', 'bonus', 'split', 'rights', 'agm', etc.
        df = client.get_actions_df(action_type="dividend")

        # Use BSE as source
        df = client.get_actions_df(source="BSE")

        # Get both NSE + BSE combined (deduped)
        df = client.get_actions_df(source="both")
    """

    def __init__(self, log_level: str = "WARNING"):
        logging.basicConfig(level=getattr(logging, log_level.upper(), logging.WARNING))
        self._nse: Optional[NSEFetcher] = None
        self._bse: Optional[BSEFetcher] = None

    @property
    def nse(self) -> NSEFetcher:
        if self._nse is None:
            self._nse = NSEFetcher()
        return self._nse

    @property
    def bse(self) -> BSEFetcher:
        if self._bse is None:
            self._bse = BSEFetcher()
        return self._bse

    # ─────────────────────────────────────────
    # Core method
    # ─────────────────────────────────────────

    def get_actions(
        self,
        symbol: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        action_type: Optional[str] = None,
        source: Literal["NSE", "BSE", "both"] = "NSE",
    ) -> List[CorporateAction]:
        """
        Fetch corporate actions.

        Args:
            symbol:      Stock symbol e.g. 'RELIANCE'. NSE only. None = all stocks.
            from_date:   'DD-MM-YYYY' for NSE, 'YYYY-MM-DD' for BSE. None = today.
            to_date:     Same format. None = 3 months ahead.
            action_type: Filter by type: 'dividend','bonus','split','rights','agm',
                         'merger','buyback','demerger','other'. None = all.
            source:      'NSE' (default), 'BSE', or 'both'.

        Returns:
            List of CorporateAction objects.
        """
        results: List[CorporateAction] = []

        if source in ("NSE", "both"):
            try:
                nse_results = self.nse.get_corporate_actions(
                    symbol=symbol, from_date=from_date, to_date=to_date
                )
                results.extend(nse_results)
                logger.info(f"NSE: fetched {len(nse_results)} actions")
            except Exception as e:
                logger.error(f"NSE fetch failed: {e}")

        if source in ("BSE", "both"):
            try:
                bse_results = self.bse.get_corporate_actions(
                    from_date=from_date, to_date=to_date
                )
                results.extend(bse_results)
                logger.info(f"BSE: fetched {len(bse_results)} actions")
            except Exception as e:
                logger.error(f"BSE fetch failed: {e}")

        # Filter by action_type if given
        if action_type:
            at = action_type.lower()
            results = [r for r in results if r.action_type == at]

        return results

    def get_actions_df(
        self,
        symbol: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        action_type: Optional[str] = None,
        source: Literal["NSE", "BSE", "both"] = "NSE",
    ) -> pd.DataFrame:
        """Same as get_actions() but returns a clean pandas DataFrame."""
        actions = self.get_actions(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
            action_type=action_type,
            source=source,
        )
        if not actions:
            return pd.DataFrame()

        df = pd.DataFrame([vars(a) for a in actions])

        # Reorder columns nicely
        col_order = ["symbol", "company_name", "action_type", "ex_date",
                     "record_date", "details", "series", "face_value",
                     "bc_start_date", "bc_end_date", "source"]
        df = df[[c for c in col_order if c in df.columns]]

        return df.reset_index(drop=True)

    # ─────────────────────────────────────────
    # Convenience shortcuts
    # ─────────────────────────────────────────

    def get_dividends(self, **kwargs) -> pd.DataFrame:
        """Get upcoming dividend announcements."""
        return self.get_actions_df(action_type="dividend", **kwargs)

    def get_bonus(self, **kwargs) -> pd.DataFrame:
        """Get upcoming bonus issue announcements."""
        return self.get_actions_df(action_type="bonus", **kwargs)

    def get_splits(self, **kwargs) -> pd.DataFrame:
        """Get upcoming stock split announcements."""
        return self.get_actions_df(action_type="split", **kwargs)

    def get_rights(self, **kwargs) -> pd.DataFrame:
        """Get upcoming rights issue announcements."""
        return self.get_actions_df(action_type="rights", **kwargs)

    def get_buybacks(self, **kwargs) -> pd.DataFrame:
        """Get upcoming buyback announcements."""
        return self.get_actions_df(action_type="buyback", **kwargs)

    # ─────────────────────────────────────────
    # NSE-only extras
    # ─────────────────────────────────────────

    def get_upcoming_results(self) -> pd.DataFrame:
        """Get upcoming quarterly result dates from NSE."""
        data = self.nse.get_upcoming_results()
        return pd.DataFrame(data) if data else pd.DataFrame()

    def get_board_meetings(
        self, from_date: Optional[str] = None, to_date: Optional[str] = None
    ) -> pd.DataFrame:
        """Get upcoming board meeting dates from NSE."""
        data = self.nse.get_board_meetings(from_date=from_date, to_date=to_date)
        return pd.DataFrame(data) if data else pd.DataFrame()

    def get_announcements(self, symbol: Optional[str] = None) -> pd.DataFrame:
        """Get latest corporate announcements from NSE."""
        data = self.nse.get_announcements(symbol=symbol)
        return pd.DataFrame(data) if data else pd.DataFrame()
