# app/ml/fraud_detector.py
"""
Fraud Detection Engine
- Click fraud: bots, click farms, duplicate clicks
- Feed fraud: fake price updates, competitor sabotage
- OCR fraud: spam submissions, fake price tags
"""
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
from sklearn.ensemble import IsolationForest


class FraudDetector:
    """
    Multi-layer fraud detection for PCE platform.
    """
    
    def __init__(self):
        self.click_model = IsolationForest(contamination=0.05, random_state=42)
        self.feed_model = IsolationForest(contamination=0.02, random_state=42)

    async def check_click_fraud(self, click_data: Dict) -> Dict:
        """
        Detect fraudulent affiliate clicks.
        """
        flags = []
        risk = 0.0
        
        # Signal 1: Click velocity from same device
        if click_data.get("clicks_last_hour", 0) > 20:
            flags.append("Excessive click velocity")
            risk += 0.25
        
        # Signal 2: Click-to-conversion time
        if click_data.get("conversion_time_seconds"):
            if click_data["conversion_time_seconds"] < 5:
                flags.append("Impossible conversion time")
                risk += 0.4
        
        # Signal 3: IP reputation (simplified)
        if click_data.get("ip_address") in self._known_bad_ips():
            flags.append("Known bad IP")
            risk += 0.5
        
        # Signal 4: User agent anomalies
        ua = click_data.get("user_agent", "")
        if "bot" in ua.lower() or "crawler" in ua.lower():
            flags.append("Bot user agent")
            risk += 0.3
        
        # Signal 5: Geographic inconsistency
        # (Would integrate with IP geolocation service)
        
        return {
            "is_fraudulent": risk > 0.5,
            "risk_score": round(min(risk, 1.0), 2),
            "flags": flags,
            "action": "block" if risk > 0.7 else "review" if risk > 0.3 else "allow",
        }

    async def check_feed_fraud(self, feed_data: Dict) -> Dict:
        """
        Detect fraudulent partner feed updates.
        """
        flags = []
        risk = 0.0
        
        # Signal 1: Extreme price changes
        old_price = feed_data.get("old_price", 0)
        new_price = feed_data.get("new_price", 0)
        if old_price > 0:
            change_pct = abs(new_price - old_price) / old_price
            if change_pct > 0.5:  # >50% change
                flags.append(f"Extreme price change ({change_pct*100:.0f}%)")
                risk += 0.3
        
        # Signal 2: Stock flip-flopping
        if feed_data.get("stock_changes_last_hour", 0) > 5:
            flags.append("Suspicious stock status changes")
            risk += 0.2
        
        # Signal 3: Off-hours updates
        hour = datetime.now().hour
        if hour < 6 or hour > 23:
            flags.append("Off-hours feed update")
            risk += 0.1
        
        return {
            "is_fraudulent": risk > 0.5,
            "risk_score": round(min(risk, 1.0), 2),
            "flags": flags,
            "action": "quarantine" if risk > 0.6 else "flag" if risk > 0.2 else "accept",
        }

    def _known_bad_ips(self) -> set:
        """Return set of known bad IP ranges."""
        # In production: integrate with IP reputation service
        return set()
