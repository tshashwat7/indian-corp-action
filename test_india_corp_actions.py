"""Tests for india_corp_actions library."""
import pytest
from unittest.mock import MagicMock, patch
from india_corp_actions import IndiaCorpActions, CorporateAction
from india_corp_actions.models import classify_action


# ─── classify_action tests ────────────────────────────────────────
class TestClassifyAction:
    def test_dividend(self):
        assert classify_action("Interim Dividend - Rs 5 Per Share") == "dividend"

    def test_bonus(self):
        assert classify_action("Bonus Issue 1:1") == "bonus"

    def test_split(self):
        assert classify_action("Stock Split from Rs 10 to Rs 2") == "split"

    def test_rights(self):
        assert classify_action("Rights Issue 1:5 at Rs 100") == "rights"

    def test_agm(self):
        assert classify_action("AGM / Annual General Meeting") == "agm"

    def test_buyback(self):
        assert classify_action("Buy Back of shares") == "buyback"

    def test_merger(self):
        assert classify_action("Amalgamation with subsidiary") == "merger"

    def test_demerger(self):
        assert classify_action("De-merger of telecom division") == "demerger"

    def test_other(self):
        assert classify_action("Something completely unknown") == "other"


# ─── NSE parsing tests ────────────────────────────────────────────
class TestNSEParsing:
    def setup_method(self):
        with patch("india_corp_actions.nse.create_nse_session") as mock_session:
            mock_session.return_value = MagicMock()
            from india_corp_actions.nse import NSEFetcher
            self.fetcher = NSEFetcher()
            self.fetcher.session = MagicMock()

    def test_parse_valid_record(self):
        raw = [{
            "symbol": "TCS",
            "comp": "Tata Consultancy Services Ltd",
            "subject": "Interim Dividend - Rs 9 Per Share",
            "exDate": "12-01-2025",
            "recDate": "13-01-2025",
            "bcStartDt": "",
            "bcEndDt": "",
            "series": "EQ",
            "faceVal": "1",
        }]
        actions = self.fetcher._parse(raw)
        assert len(actions) == 1
        assert actions[0].symbol == "TCS"
        assert actions[0].action_type == "dividend"
        assert actions[0].source == "NSE"

    def test_parse_empty(self):
        assert self.fetcher._parse([]) == []

    def test_parse_malformed_skipped(self):
        raw = [None, {}, {"symbol": "X", "comp": "Y", "subject": "Bonus"}]
        actions = self.fetcher._parse(raw)
        assert len(actions) >= 1


# ─── Client integration tests (mocked) ───────────────────────────
class TestClient:
    @patch("india_corp_actions.client.NSEFetcher")
    def test_get_dividends_returns_df(self, MockNSE):
        mock_nse = MockNSE.return_value
        mock_nse.get_corporate_actions.return_value = [
            CorporateAction(
                symbol="INFY", company_name="Infosys", action_type="dividend",
                ex_date="15-03-2025", record_date="16-03-2025",
                bc_start_date="", bc_end_date="",
                details="Final Dividend Rs 20", source="NSE"
            )
        ]

        client = IndiaCorpActions()
        client._nse = mock_nse

        df = client.get_dividends()
        assert not df.empty
        assert df.iloc[0]["symbol"] == "INFY"
        assert df.iloc[0]["action_type"] == "dividend"

    @patch("india_corp_actions.client.NSEFetcher")
    def test_empty_result_returns_empty_df(self, MockNSE):
        mock_nse = MockNSE.return_value
        mock_nse.get_corporate_actions.return_value = []
        client = IndiaCorpActions()
        client._nse = mock_nse
        df = client.get_actions_df()
        assert df.empty
