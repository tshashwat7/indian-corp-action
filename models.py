from dataclasses import dataclass, field
from typing import Optional
from datetime import date


@dataclass
class CorporateAction:
    """Represents a single corporate action event."""
    symbol: str
    company_name: str
    action_type: str          # dividend, bonus, split, rights, agm, etc.
    ex_date: Optional[str]    # Ex-date (most important for trading)
    record_date: Optional[str]
    bc_start_date: Optional[str]  # Book closure start
    bc_end_date: Optional[str]    # Book closure end
    details: str              # Raw description e.g. "Dividend - Rs 5 Per Share"
    source: str               # "NSE" or "BSE"
    series: Optional[str] = None
    face_value: Optional[str] = None

    def __repr__(self):
        return (f"CorporateAction(symbol={self.symbol!r}, type={self.action_type!r}, "
                f"ex_date={self.ex_date!r}, details={self.details!r})")


ACTION_TYPE_MAP = {
    # demerger must come BEFORE merger/dividend so "de-merger" / "demerger" doesn't
    # accidentally match "merger" or "div" inside other patterns
    "demerger": ["demerger", "de-merger", "spin-off", "spinoff"],
    "dividend": ["dividend", "interim dividend", "final dividend", "special dividend"],
    "bonus": ["bonus"],
    "split": ["split", "sub-division", "subdivision"],
    "rights": ["rights"],
    "agm": ["agm", "annual general meeting"],
    "egm": ["egm", "extraordinary general meeting"],
    "buyback": ["buyback", "buy back"],
    "merger": ["merger", "amalgamation", "scheme"],
}


def classify_action(description: str) -> str:
    """Classify a raw action description into a clean type."""
    desc_lower = description.lower()
    for action_type, keywords in ACTION_TYPE_MAP.items():
        if any(kw in desc_lower for kw in keywords):
            return action_type
    return "other"
