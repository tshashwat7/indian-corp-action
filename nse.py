import logging
from typing import List, Optional
from datetime import datetime, timedelta

from .utils import create_nse_session, safe_get
from .models import CorporateAction, classify_action

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com/api"

# NSE corporate action endpoint supports these index types
NSE_INDICES = ["equities", "sme", "debt"]


class NSEFetcher:
    """Fetches corporate actions data from NSE India."""

    def __init__(self):
        self.session = None

    def _ensure_session(self):
        if self.session is None:
            logger.info("Initializing NSE session...")
            self.session = create_nse_session()
            if self.session is None:
                raise ConnectionError(
                    "Could not establish NSE session. Check your internet connection."
                )

    def get_corporate_actions(
        self,
        symbol: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        index: str = "equities",
    ) -> List[CorporateAction]:
        """
        Fetch corporate actions from NSE.

        Args:
            symbol: Stock symbol e.g. 'RELIANCE', 'TCS'. None = all stocks.
            from_date: Start date as 'DD-MM-YYYY'. Defaults to today.
            to_date: End date as 'DD-MM-YYYY'. Defaults to 3 months ahead.
            index: 'equities' (default), 'sme', or 'debt'.

        Returns:
            List of CorporateAction objects.
        """
        self._ensure_session()

        # Default date range: today → 3 months ahead
        today = datetime.today()
        if from_date is None:
            from_date = today.strftime("%d-%m-%Y")
        if to_date is None:
            to_date = (today + timedelta(days=90)).strftime("%d-%m-%Y")

        url = f"{NSE_BASE}/corporates-corporateActions"
        params = {
            "index": index,
            "from_date": from_date,
            "to_date": to_date,
        }
        if symbol:
            params["symbol"] = symbol.upper()

        data = safe_get(self.session, url, params=params)
        if data is None:
            logger.warning("NSE returned no data.")
            return []

        return self._parse(data)

    def get_upcoming_results(self) -> List[dict]:
        """Fetch upcoming quarterly result dates from NSE."""
        self._ensure_session()
        url = f"{NSE_BASE}/corporate-results-forthcoming"
        data = safe_get(self.session, url)
        if not data:
            return []
        return data if isinstance(data, list) else data.get("data", [])

    def get_board_meetings(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[dict]:
        """Fetch upcoming board meeting announcements."""
        self._ensure_session()
        today = datetime.today()
        if from_date is None:
            from_date = today.strftime("%d-%m-%Y")
        if to_date is None:
            to_date = (today + timedelta(days=90)).strftime("%d-%m-%Y")

        url = f"{NSE_BASE}/corporate-board-meetings"
        params = {"index": "equities", "from_date": from_date, "to_date": to_date}
        data = safe_get(self.session, url, params=params)
        return data if isinstance(data, list) else (data or {}).get("data", [])

    def get_announcements(self, symbol: Optional[str] = None) -> List[dict]:
        """Fetch latest corporate announcements from NSE."""
        self._ensure_session()
        url = f"{NSE_BASE}/corporate-announcements"
        params = {"index": "equities"}
        if symbol:
            params["symbol"] = symbol.upper()
        data = safe_get(self.session, url, params=params)
        return data if isinstance(data, list) else []

    def _parse(self, data) -> List[CorporateAction]:
        """Parse raw NSE JSON response into CorporateAction objects."""
        results = []
        if isinstance(data, dict):
            data = data.get("data", data.get("corporateActions", []))

        for item in (data or []):
            try:
                details = item.get("subject", item.get("purpose", ""))
                action = CorporateAction(
                    symbol=item.get("symbol", ""),
                    company_name=item.get("comp", item.get("companyName", "")),
                    action_type=classify_action(details),
                    ex_date=item.get("exDate", item.get("exDt", "")),
                    record_date=item.get("recDate", item.get("recDt", "")),
                    bc_start_date=item.get("bcStartDt", ""),
                    bc_end_date=item.get("bcEndDt", ""),
                    details=details,
                    series=item.get("series", "EQ"),
                    face_value=item.get("faceVal", ""),
                    source="NSE",
                )
                results.append(action)
            except Exception as e:
                logger.debug(f"Skipping malformed NSE record: {e}")

        return results
