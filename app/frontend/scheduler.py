from extras.tasks import scrape_offer

PROXIES = [
    "http://user:pass@proxy1.residential-provider.com:8080",
    "http://user:pass@proxy2.residential-provider.com:8080",
]

def trigger_price_updates():
    # Fetch active vendor offers needing price checks from PostgreSQL
    offers_to_update = [
        {"id": "off_1", "url": "https://site-a.com/item/101"},
        {"id": "off_2", "url": "https://site-b.com/product/202"}
    ]
    
    for idx, offer in enumerate(offers_to_update):
        # Rotate proxy assignment per task dispatch
        proxy = PROXIES[idx % len(PROXIES)]
        scrape_offer.delay(offer["id"], offer["url"], proxy)

if __name__ == "__main__":
    trigger_price_updates()