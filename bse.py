import logging
from typing import List, Optional
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from .utils import create_bse_session, safe_get
from .models import CorporateAction, classify_action

logger = logging.getLogger(__name__)

BSE_BASE = "https://api.bseindia.com/BseIndiaAPI/api"


class BSEFetcher:
    """Fetches corporate actions data from BSE India."""

    def __init__(self):
        self.session = create_bse_session()

    def get_corporate_actions(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        action_type: str = "All",
    ) -> List[CorporateAction]:
        """
        Fetch corporate actions from BSE.

        Args:
            from_date: 'YYYY-MM-DD'. Defaults to today.
            to_date: 'YYYY-MM-DD'. Defaults to 3 months ahead.
            action_type: 'All', 'Dividend', 'Bonus', 'Split', 'Rights', etc.

        Returns:
            List of CorporateAction objects.
        """
        today = datetime.today()
        if from_date is None:
            from_date = today.strftime("%Y%m%d")
        else:
            from_date = from_date.replace("-", "")

        if to_date is None:
            to_date = (today + timedelta(days=90)).strftime("%Y%m%d")
        else:
            to_date = to_date.replace("-", "")

        url = f"{BSE_BASE}/CorporateAction/w"
        params = {
            "strCat": action_type,
            "strPrevDate": from_date,
            "strScrip": "",
            "strSearch": "S",
            "strToDate": to_date,
            "strType": "C",
            "scripcode": "",
        }

        data = safe_get(self.session, url, params=params)
        if data is None:
            logger.warning("BSE returned no data.")
            return []

        return self._parse(data)

    def get_dividends(self, from_date=None, to_date=None) -> List[CorporateAction]:
        return self.get_corporate_actions(from_date, to_date, action_type="D")

    def get_bonus(self, from_date=None, to_date=None) -> List[CorporateAction]:
        return self.get_corporate_actions(from_date, to_date, action_type="B")

    def get_splits(self, from_date=None, to_date=None) -> List[CorporateAction]:
        return self.get_corporate_actions(from_date, to_date, action_type="S")

    def get_rights(self, from_date=None, to_date=None) -> List[CorporateAction]:
        return self.get_corporate_actions(from_date, to_date, action_type="R")

    def _parse(self, data) -> List[CorporateAction]:
        """Parse BSE JSON into CorporateAction objects."""
        results = []
        records = data if isinstance(data, list) else data.get("Table", [])

        for item in (records or []):
            try:
                details = item.get("Purpose", item.get("SUBJECTLINE", ""))
                action = CorporateAction(
                    symbol=item.get("SCRIP_CD", item.get("scripcode", "")),
                    company_name=item.get("SCRIP_NAME", item.get("LONG_NAME", "")),
                    action_type=classify_action(details),
                    ex_date=item.get("EX_DATE", item.get("ExDate", "")),
                    record_date=item.get("ND_END_DT", item.get("RecordDate", "")),
                    bc_start_date=item.get("ND_START_DT", ""),
                    bc_end_date=item.get("ND_END_DT", ""),
                    details=details,
                    face_value=item.get("Face_Value", ""),
                    source="BSE",
                )
                results.append(action)
            except Exception as e:
                logger.debug(f"Skipping malformed BSE record: {e}")

        return results
