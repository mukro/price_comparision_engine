# LEGAL_COMPLIANCE_CHECKLIST.md
# Price Comparison Engine — Legal Compliance Framework
# Jurisdiction: India (primary) | Global considerations included
# Last Updated: 2026-07-31

---

## 📋 Executive Summary

This document serves as a living legal compliance checklist for the Price Comparison Engine. It covers the four-tier data acquisition strategy (API → Affiliate/User-OCR → Merchant Partner → Scraping) and provides actionable items for each jurisdiction and data source.

**Review Cycle:** Quarterly, or immediately upon:
- New vendor/domain addition
- Change in applicable law (IT Act amendments, DPDP Act, etc.)
- Receipt of takedown notice
- Expansion to new country/region

---

## 🇮🇳 India-Specific Legal Framework

### 1. Information Technology Act, 2000

| Section | Requirement | Our Compliance | Evidence |
|---------|-------------|----------------|----------|
| **Section 43** | No unauthorized access to computer systems | ✅ Scraping only after good-faith API negotiation; compliance gatekeeper blocks disallowed domains | `compliance.py` logs, `scrape_attempt` audit trail |
| **Section 66** | No hacking / data theft | ✅ No credential stuffing, no CAPTCHA bypass, no circumvention of technical barriers | Architecture docs, code review |
| **Section 69** | Intermediary compliance | ✅ We qualify as "intermediary" under Section 2(1)(w) | Terms of Service, takedown procedure |
| **Section 79** | Safe harbor for intermediaries | ✅ Must NOT: (a) initiate transmission, (b) select receiver, (c) modify content | User-OCR tier design, data flow diagrams |
| **IT Rules 2021** | Due diligence for intermediaries | ✅ Grievance officer, 24hr takedown response, monthly compliance report | `GRIEVANCE_OFFICER.md` |

### 2. Digital Personal Data Protection Act, 2023 (DPDP)

| Principle | Requirement | Our Compliance |
|-----------|-------------|----------------|
| **Consent** | Explicit consent for personal data | ✅ User-OCR submissions require explicit opt-in during onboarding |
| **Purpose Limitation** | Data used only for stated purpose | ✅ OCR data used ONLY for price comparison; no behavioral profiling |
| **Data Minimization** | Collect only what's necessary | ✅ Store: price, product_id, vendor, timestamp, geo_hash. Do NOT store: user name, full address, device ID |
| **Storage Limitation** | Delete when no longer needed | ✅ Auto-purge raw screenshots after OCR extraction; price data archived after 90 days |
| **Accuracy** | Ensure data accuracy | ✅ Verification score system; community validation |
| **Security** | Reasonable security safeguards | ✅ Encryption at rest (KMS), TLS in transit, RBAC |
| **Accountability** | Demonstrate compliance | ✅ This checklist, audit logs, DPO appointment |

### 3. Competition Act, 2002

| Concern | Mitigation |
|---------|------------|
| **Predatory pricing allegations** | We do NOT set prices; we only display them. Merchants set their own prices. |
| **Cartel facilitation** | No price-fixing features. Dynamic pricing is merchant-specific and opt-in. |
| **Abuse of dominance** | We are a price aggregator, not a marketplace. No control over inventory or transactions. |

### 4. Consumer Protection Act, 2019

| Requirement | Compliance |
|-------------|------------|
| **No misleading advertisements** | All prices clearly labeled with source, timestamp, and verification status |
| **Refund/return policy display** | We link to merchant policies; we do NOT handle transactions |
| **Grievance redressal** | 24-hour response commitment; dedicated grievance officer |

---

## 🌍 Global Considerations

### United States (CFAA, State Laws)

| Risk | Mitigation |
|------|------------|
| **CFAA § 1030(a)(2)** — Unauthorized access | Never scrape sites requiring login. Never bypass bot detection. Document robots.txt compliance. |
| **California CCPA** | No California resident PII stored without opt-out mechanism. |
| **HiQ v. LinkedIn precedent** | Public data scraping is not CFAA violation, but ToS violation remains civil risk. We mitigate by prioritizing APIs and user submissions. |

### European Union (GDPR)

| Risk | Mitigation |
|------|------------|
| **GDPR Article 6 — Lawful basis** | Legitimate interest for price data (publicly advertised prices). Consent for any personal data. |
| **GDPR Article 17 — Right to erasure** | Instant domain-level takedown capability. |
| **GDPR Article 20 — Data portability** | Users can export their alert history via API. |

---

## 🏗️ Four-Tier Data Acquisition: Legal Checklist

### TIER 1: Official APIs

```
Status: 🟢 LEGALLY BULLETPROOF (with proper ToS compliance)
```

- [ ] **API Agreement Documentation**
  - [ ] Signed/data API terms of service for each vendor
  - [ ] Rate limit compliance documented
  - [ ] Attribution requirements implemented
  - [ ] No-resale clauses reviewed by counsel
  - [ ] API key rotation schedule (quarterly)

- [ ] **Amazon Associates / Flipkart Affiliate**
  - [ ] Affiliate account active and in good standing
  - [ ] Affiliate tags appended to all outbound URLs
  - [ ] Commission disclosure on website/app

- [ ] **Quick Commerce APIs**
  - [ ] Blinkit partner API: application submitted / approved
  - [ ] Zepto merchant API: application submitted / approved
  - [ ] Swiggy Instamart partner API: application submitted / approved
  - [ ] Dunzo API: application submitted / approved

- [ ] **Travel APIs**
  - [ ] Amadeus / Sabre / Travelport GDS access (if applicable)
  - [ ] OTA affiliate APIs: MakeMyTrip, Goibibo, Booking.com
  - [ ] Airline direct APIs: IndiGo, Air India (if public)

- [ ] **Cab Hailing APIs**
  - [ ] Uber API: developer account + ride estimates endpoint
  - [ ] Ola API: developer account + fare estimates endpoint
  - [ ] Rapido API: partner application submitted

### TIER 2: Affiliate / User OCR (Social Mode)

```
Status: 🟡 LEGALLY DEFENSIBLE (with proper TOS and anonymization)
```

- [ ] **User Terms of Service**
  - [ ] Explicit clause: user grants perpetual, royalty-free license to process and display submitted price data
  - [ ] Explicit clause: user confirms they have right to share the screenshot (personal use doctrine)
  - [ ] Explicit clause: we may anonymize and aggregate submissions
  - [ ] Opt-out mechanism: user can delete all their submissions

- [ ] **Privacy by Design (On-Device OCR)**
  - [ ] ✅ Raw screenshots NEVER leave the device
  - [ ] ✅ Only extracted text/numbers transmitted to server
  - [ ] ✅ No EXIF metadata transmitted (location, device model stripped)
  - [ ] ✅ Device fingerprint is one-way hashed (not reversible)

- [ ] **Data Handling**
  - [ ] Server receives: `{price, currency, product_name, vendor_domain, timestamp, geo_hash, device_hash}`
  - [ ] Server does NOT receive: raw image, GPS coordinates, device ID, phone number
  - [ ] Retention: raw submissions purged after 24 hours; only aggregated price data retained

- [ ] **Community Moderation**
  - [ ] Report false price button on every listing
  - [ ] Auto-flag outliers (>30% deviation from median)
  - [ ] Reputation system for frequent contributors
  - [ ] Ban mechanism for gaming/manipulation

- [ ] **Copyright Consideration**
  - [ ] Screenshots are factual data (prices) — minimal creative expression
  - [ ] No reproduction of product images, descriptions, or branding beyond necessary price context
  - [ ] Fair dealing defense documented (Section 52, Copyright Act)

### TIER 3: Merchant Partner Feeds

```
Status: 🟢 LEGALLY BULLETPROOF (contractual basis)
```

- [ ] **Partner Agreement Template**
  - [ ] Data license: merchant grants right to display prices/offers
  - [ ] Accuracy warranty: merchant warrants data is accurate and current
  - [ ] Update frequency: minimum refresh rate specified
  - [ ] Termination: either party can terminate with 30 days notice
  - [ ] Liability cap: merchant liable for inaccurate pricing; platform liable for display errors only

- [ ] **Partner Onboarding**
  - [ ] KYC verification for each merchant partner
  - [ ] API key issuance with rate limits
  - [ ] Dashboard access for self-service offer management
  - [ ] Webhook endpoint for real-time price updates

- [ ] **Revenue Model Documentation**
  - [ ] SaaS subscription for premium partners
  - [ ] CPC/CPA affiliate model for traffic referrals
  - [ ] Commission structure transparent and documented

### TIER 4: Scraping (Last Resort)

```
Status: 🟠 LEGALLY RISKY (mitigated but not eliminated)
```

- [ ] **Good Faith Negotiation Log**
  - [ ] For each scraped domain, document:
    - [ ] Date of API/partnership inquiry email sent
    - [ ] Recipient email address and name
    - [ ] Response (or lack thereof after 14 days)
    - [ ] robots.txt status at time of first scrape
  - [ ] Log stored in `scrape_dlq` table with `negotiation_attempted` flag

- [ ] **Technical Compliance**
  - [ ] robots.txt checked before every scrape (Protego)
  - [ ] Crawl-delay respected if specified
  - [ ] Rate limit: max 6 RPM per domain (configurable)
  - [ ] User-agent clearly identifies bot: `PriceComparisonBot/1.0`
  - [ ] No login-required pages scraped
  - [ ] No CAPTCHA bypass attempted
  - [ ] No JavaScript obfuscation circumvented

- [ ] **Takedown Response System**
  - [ ] Dedicated email: legal@yourdomain.com
  - [ ] 24-hour acknowledgment SLA
  - [ ] 72-hour removal SLA
  - [ ] Automated domain block capability (`ENFORCE_DOMAIN_ALLOWLIST`)
  - [ ] All takedown requests logged with timestamp and resolution

- [ ] **Data Minimization**
  - [ ] Only price, availability, product identifier, URL stored
  - [ ] No user reviews, images, or descriptive text scraped
  - [ ] No personal data of merchant customers collected

---

## 📑 Required Legal Documents

### Must-Have (Before Launch)

| Document | Purpose | Owner |
|----------|---------|-------|
| **Terms of Service** | User agreement, data license, liability limitation | Legal counsel |
| **Privacy Policy** | DPDP/GDPR compliance, data handling practices | Legal + DPO |
| **Cookie Policy** | If using analytics cookies | Legal |
| **Grievance Officer Appointment** | IT Rules 2021 requirement | Board resolution |
| **Merchant Partner Agreement** | Tier 3 contractual basis | Legal + BD |
| **API Compliance Matrix** | Tier 1 vendor ToS tracking | Compliance officer |

### Should-Have (Within 90 Days)

| Document | Purpose |
|----------|---------|
| **Data Retention Policy** | How long each data type is kept |
| **Incident Response Plan** | Data breach procedures |
| **Vendor Risk Assessment** | Annual review of each data source |
| **User Content Moderation Policy** | How OCR submissions are validated |
| **Competition Law Compliance Guide** | Internal training for team |

---

## 🚨 Takedown Response Playbook

### Step 1: Receive Notice
- Channel: legal@yourdomain.com (monitored 24/7)
- Required info: domain, specific URLs, legal basis for request, contact details

### Step 2: Acknowledge (Within 24 hours)
```
Subject: Takedown Request Acknowledged — [Domain]

Dear [Requester],

We have received your request regarding [domain]. We are reviewing it and will respond within 72 hours.

Request ID: TK-[UUID]
```

### Step 3: Evaluate (Within 48 hours)
- Is the request from authorized domain owner?
- Is the data from Tier 1 (API)? → Coordinate with API team, do NOT remove without API partner discussion
- Is the data from Tier 2 (User OCR)? → Evaluate if submission violates our TOS
- Is the data from Tier 3 (Partner)? → Coordinate with merchant success team
- Is the data from Tier 4 (Scraped)? → **IMMEDIATE BLOCK** — set `scraping_allowed = FALSE` for domain

### Step 4: Execute (Within 72 hours)
```sql
-- Immediate domain block
UPDATE vendors SET scraping_allowed = FALSE, is_active = FALSE WHERE domain = 'requested-domain.com';
-- Remove from cache
DELETE FROM vendor_offers WHERE vendor_id IN (SELECT id FROM vendors WHERE domain = 'requested-domain.com');
```

### Step 5: Respond
```
Subject: Takedown Request Resolved — [Domain]

Dear [Requester],

We have [blocked/removed] all data from [domain] as requested. 

Action taken: [Detailed description]
Effective date: [Timestamp]

If you have any questions, please contact us at legal@yourdomain.com.
```

### Step 6: Internal Review
- Log in `takedown_log` table
- Quarterly review: are there patterns? (e.g., multiple takedowns from same industry)
- Adjust tier strategy if needed

---

## 📊 Compliance Metrics Dashboard

Track these KPIs monthly:

| Metric | Target | Measurement |
|--------|--------|-------------|
| % prices from Tier 1 (API) | > 60% | `data_source = 'official_api'` |
| % prices from Tier 2 (User OCR) | < 30% | `data_source = 'user_ocr'` |
| % prices from Tier 3 (Partner) | > 10% | `data_source = 'merchant_partner'` |
| % prices from Tier 4 (Scraped) | < 5% | `data_source = 'scraped'` |
| Takedown requests received | 0 | Legal inbox |
| Takedown response time | < 24 hours | `takedown_log` table |
| robots.txt compliance rate | 100% | `compliance.py` audit |
| API ToS violations | 0 | Partner communications |

---

## ✅ Pre-Launch Legal Sign-Off Checklist

- [ ] All 4 tiers documented and approved by legal counsel
- [ ] Terms of Service reviewed by lawyer and published
- [ ] Privacy Policy reviewed by lawyer and published
- [ ] Grievance officer appointed and contact published
- [ ] DPDP compliance assessment completed
- [ ] All Tier 1 API agreements signed and filed
- [ ] Tier 2 user TOS includes explicit data license
- [ ] Tier 3 partner agreement template approved by legal
- [ ] Tier 4 scraping log system operational
- [ ] Takedown response playbook tested (dry run)
- [ ] Cyber insurance policy active (covers data breach liability)
- [ ] D&O insurance covers IP infringement claims

---

**Document Owner:** [Your Name]  
**Legal Counsel:** [Lawyer Name / Firm]  
**Last Review:** 2026-07-31  
**Next Review:** 2026-10-31
