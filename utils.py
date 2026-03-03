import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.bseindia.com/",
}


def create_nse_session(retries: int = 3, delay: float = 1.0) -> Optional[requests.Session]:
    """
    Create an authenticated NSE session by warming up cookies via homepage visit.
    NSE blocks direct API calls without a valid session cookie.
    """
    session = requests.Session()
    session.headers.update(NSE_HEADERS)

    urls_to_warm = [
        "https://www.nseindia.com",
        "https://www.nseindia.com/market-data/live-equity-market",
    ]

    for attempt in range(retries):
        try:
            for url in urls_to_warm:
                resp = session.get(url, timeout=10)
                resp.raise_for_status()
                time.sleep(delay)
            logger.debug("NSE session established successfully.")
            return session
        except requests.RequestException as e:
            logger.warning(f"NSE session attempt {attempt + 1} failed: {e}")
            time.sleep(delay * (attempt + 1))

    logger.error("Failed to establish NSE session after retries.")
    return None


def create_bse_session() -> requests.Session:
    """Create a simple BSE session."""
    session = requests.Session()
    session.headers.update(BSE_HEADERS)
    return session


def safe_get(session: requests.Session, url: str, params: dict = None,
             retries: int = 3, delay: float = 1.5) -> Optional[dict]:
    """GET request with retry logic, returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP {e.response.status_code} on attempt {attempt + 1} for {url}")
            if e.response.status_code == 401:
                logger.error("Session expired. Re-initialize the client.")
                return None
        except requests.RequestException as e:
            logger.warning(f"Request error attempt {attempt + 1}: {e}")
        time.sleep(delay * (attempt + 1))
    return None
