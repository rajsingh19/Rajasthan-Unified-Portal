"""
myscheme_scraper.py - Improved V4
Tries multiple API/HTML extraction paths and explicitly marks whether the
returned dataset is live or fallback.
"""
import re, json, logging, requests, urllib3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

urllib3.disable_warnings()
log = logging.getLogger("scraper.myscheme")

BASE_URL = "https://www.myscheme.gov.in"
API_BASE = "https://api.myscheme.gov.in"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "myscheme_schemes.json"

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": BASE_URL, 
    "Referer": f"{BASE_URL}/search/state/Rajasthan",
}
API_KEY_HEADERS = {
    **BASE_HEADERS,
    # MyScheme has historically required a client key for some search endpoints.
    "x-api-key": "tYTy5eEhlu9rFjyxuCr7ra7ACp4dv1RH8gWuHTDc",
}

CAT_MAP = {
    r"health|medical|ayush|hospital|chiranjeevi|ayushman": "Health",
    r"education|school|scholarsh|student|coaching":        "Education",
    r"agriculture|kisan|farm|crop|irrigation":             "Agriculture",
    r"social|pension|widow|disable|palanhar|welfare":      "Social Welfare",
    r"women|mahila|girl|beti|maternity|ladli":             "Women & Child",
    r"labour|worker|employment|rozgar|skill|training":     "Labour & Employment",
    r"business|msme|startup|enterprise|loan|mudra":        "Business & Finance",
    r"housing|awas|shelter":                               "Housing",
    r"food|ration|rasoi|pds|nutrition":                    "Food Security",
    r"water|jal|sanitation|swachh":                        "Water & Sanitation",
    r"energy|solar|electricity|ujjwala":                   "Energy",
    r"digital|it|emitra|technology":                       "Digital Services",
    r"mining|mineral":                                     "Mining",
}

def _category(text):
    t = text.lower()
    for pat, cat in CAT_MAP.items():
        if re.search(pat, t, re.I): return cat
    return "General"

def _normalize(src, i, ts, source_mode="live", fetch_method="unknown", source_note=""):
    """Clean and normalize a raw dict from the API or __NEXT_DATA__ into our JSON output format"""
    source_data = src.get("_source", src)
    
    name = source_data.get("schemeName") or source_data.get("title") or source_data.get("name") or f"Scheme {i+1}"
    slug = source_data.get("slug") or source_data.get("schemeSlug") or source_data.get("schemeCode") or ""
    
    tags = source_data.get("tags") or source_data.get("beneficiaryType") or []
    if isinstance(tags, str): 
        tags = [tags]
        
    ministry = source_data.get("nodalMinistryName") or source_data.get("ministry") or source_data.get("department") or ""
    desc = source_data.get("briefDescription") or source_data.get("description") or source_data.get("objective") or ""
    benefit = source_data.get("benefits") or source_data.get("benefit") or ""
    eligibility = source_data.get("eligibilityCriteria") or source_data.get("eligibility") or ""
    launched = source_data.get("launchedOn") or source_data.get("startDate") or ""
    
    # Categorize smartly
    cat = _category(name + " " + ministry + " " + " ".join(str(t) for t in tags))
    
    # Build URL link directly to the scheme page
    if slug:
        scheme_url = f"{BASE_URL}/schemes/{slug}"
    else:
        scheme_url = f"{BASE_URL}/search?q={requests.utils.quote(name[:50])}"
        
    return {
        "id": f"myscheme_{i+1}",
        "scheme_name": name.strip(),
        "category": cat,
        "ministry": ministry,
        "department": ministry, # API often combines them
        "application_link": scheme_url,
        "url": scheme_url, 
        "description": desc[:300] if desc else f"Government scheme for {cat.lower()}",
        "benefits": benefit[:200] if isinstance(benefit, str) else "",
        "eligibility": eligibility[:200] if isinstance(eligibility, str) else "",
        "launched": str(launched)[:10] if launched else "",
        "state": "Rajasthan",
        "status": "Active",
        "source": "myscheme.gov.in" if source_mode == "live" else "myscheme.gov.in (fallback)",
        "source_mode": source_mode,
        "fetch_method": fetch_method,
        "source_note": source_note or ("Live MyScheme data" if source_mode == "live" else "Curated fallback dataset"),
        "scraped_at": ts,
    }


def _extract_hits(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    candidates = [
        data.get("hits", {}).get("hits") if isinstance(data.get("hits"), dict) else None,
        data.get("schemes"),
        data.get("data"),
        data.get("results"),
        data.get("items"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and len(candidate) > 2:
            return candidate
        if isinstance(candidate, dict):
            for nested_key in ("schemes", "items", "results"):
                nested = candidate.get(nested_key)
                if isinstance(nested, list) and len(nested) > 2:
                    return nested
    return []


def _extract_next_data_schemes(next_data):
    if not isinstance(next_data, dict):
        return []

    stack = [next_data]
    seen = set()
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)

        if isinstance(node, dict):
            for key in ("schemes", "items", "results"):
                value = node.get(key)
                if isinstance(value, list) and len(value) > 2:
                    return value
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            if len(node) > 2 and all(isinstance(item, dict) for item in node):
                sample = node[0]
                if any(k in sample for k in ("schemeName", "title", "name", "slug", "_source")):
                    return node
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return []

def scrape_myscheme():
    """Main fetching logic connecting resilient methodology."""
    ts = datetime.now(timezone.utc).isoformat()
    session = requests.Session()
    
    # Encode modern structure properly for testing multiple variants 
    q_param = '[{"identifier":"level","value":"State"},{"identifier":"beneficiaryState","value":"Rajasthan"}]'
    encoded_q = urllib.parse.quote(q_param)

    # Note: Modern MyScheme aggressively 401/403s bots. We implement multiple potential endpoints.
    api_urls = [
        {"url": f"{API_BASE}/search/v6/schemes?lang=en&q={encoded_q}&keyword=&sort=multiple_sort&from=0&size=100", "method": "GET", "headers": API_KEY_HEADERS},
        {"url": f"{API_BASE}/search/v6/schemes?lang=en&q={encoded_q}&keyword=&sort=multiple_sort&from=0&size=100", "method": "GET", "headers": BASE_HEADERS},
        {"url": f"{API_BASE}/search/v5/schemes?lang=en&q={encoded_q}&keyword=&from=0&size=100", "method": "GET", "headers": API_KEY_HEADERS},
        {"url": f"{API_BASE}/search/v4/schemes?lang=en&q=&from=0&size=100", "method": "POST", "json": {"state": "Rajasthan"}, "headers": API_KEY_HEADERS},
        {"url": f"{API_BASE}/search/v4/schemes?lang=en&filters=state:Rajasthan&from=0&size=100", "method": "GET", "headers": API_KEY_HEADERS},
    ]
    
    for attempt in api_urls:
        try:
            headers = attempt.get("headers", BASE_HEADERS)
            if attempt["method"] == "GET":
                r = session.get(attempt["url"], headers=headers, timeout=10, verify=False)
            else:
                r = session.post(attempt["url"], headers=headers, json=attempt.get("json", {}), timeout=10, verify=False)
                
            if r.status_code == 200:
                data = r.json()
                hits = _extract_hits(data)
                if hits and len(hits) > 2:
                    log.info("MyScheme API OK: %d schemes extracted via %s", len(hits), attempt["url"][:80])
                    return [_normalize(h, i, ts, source_mode="live", fetch_method="api", source_note=attempt["url"]) for i, h in enumerate(hits)]
        except Exception as e:
            log.debug("MyScheme API route %s generated an exception: %s", attempt["url"][:60], e)

    # Fallback to HTML/Next.js hydration data extraction, which is still live data.
    try:
        html_url = f"{BASE_URL}/search/state/Rajasthan"
        html_headers = {**BASE_HEADERS, "Accept": "text/html"}
        r = session.get(html_url, headers=html_headers, timeout=10, verify=False)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if m:
            next_data = json.loads(m.group(1))
            schemes_data = _extract_next_data_schemes(next_data)
            
            if schemes_data and len(schemes_data) > 2:
                log.info("MyScheme __NEXT_DATA__ fallback triggered: %d schemes extracted", len(schemes_data))
                return [_normalize(s, i, ts, source_mode="live", fetch_method="html", source_note=html_url) for i, s in enumerate(schemes_data)]
    except Exception as e:
        log.error("MyScheme HTML extraction fail: %s", e)

    log.warning("MyScheme: all live fetching methods failed (blocked by 403 API), serving precise fallback dataset.")
    return _fallback(ts)


def save_json(data, output_path=OUTPUT_PATH):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Saved %d MyScheme rows to %s", len(data), output_path)

def _fallback(ts):
    """
    Highly robust fallback dataset matching the primary Rajasthan schemes required by the backend.
    Used when APIs block automated requests.
    """
    schemes = [
        ("PM Kisan Samman Nidhi",        "Agriculture",        "Ministry of Agriculture",  "₹6,000/year direct transfer", "Small & marginal farmers",         "pm-kisan-samman-nidhi"),
        ("Ayushman Bharat PM-JAY",        "Health",             "Ministry of Health",       "₹5 lakh/year health insurance","BPL families",                     "ayushman-bharat-pradhan-mantri-jan-arogya-yojana"),
        ("PM Awas Yojana Gramin",         "Housing",            "Ministry of Rural Dev.",   "₹1.2 lakh per rural house",   "Homeless rural families",          "pradhan-mantri-awaas-yojana-gramin"),
        ("MGNREGA",                       "Labour & Employment","Ministry of Rural Dev.",   "100 days guaranteed wages",   "Rural households",                 "mahatma-gandhi-national-rural-employment-guarantee-act"),
        ("PM Ujjwala Yojana",             "Energy",             "Ministry of Petroleum",    "Free LPG connection",         "BPL women",                        "pradhan-mantri-ujjwala-yojana"),
        ("Sukanya Samriddhi Yojana",      "Women & Child",      "Ministry of Finance",      "Tax-free savings for girl",   "Parents of girl child <10yrs",     "sukanya-samriddhi-yojana"),
        ("PM Fasal Bima Yojana",          "Agriculture",        "Ministry of Agriculture",  "Crop insurance",              "Farmers",                          "pradhan-mantri-fasal-bima-yojana"),
        ("PM Mudra Yojana",               "Business & Finance", "Ministry of Finance",      "Loans up to ₹10 lakh",        "Small entrepreneurs",              "pradhan-mantri-mudra-yojana"),
        ("National Apprenticeship",       "Labour & Employment","Ministry of Skill Dev.",   "Stipend + training",          "Youth 14-21 years",                "national-apprenticeship-promotion-scheme"),
        ("PM SVANidhi",                   "Business & Finance", "Ministry of Housing",      "Working capital loan",        "Street vendors",                   "pradhan-mantri-svanidhi"),
        ("Stand-Up India",                "Business & Finance", "Ministry of Finance",      "₹10L–₹1Cr loan",             "SC/ST/Women entrepreneurs",        "stand-up-india"),
        ("Jal Jeevan Mission",            "Water & Sanitation", "Ministry of Jal Shakti",   "Tap water to every HH",       "Rural households",                 "jal-jeevan-mission"),
        ("PM Poshan",                     "Education",          "Ministry of Education",    "Free mid-day meals",          "School children",                  "pradhan-mantri-poshan-shakti-nirman"),
        ("Digital India",                 "Digital Services",   "Ministry of IT",           "Digital infrastructure",      "All citizens",                     "digital-india"),
        ("Atal Pension Yojana",           "Social Welfare",     "Ministry of Finance",      "Pension ₹1000–5000/month",    "Unorganised sector workers",       "atal-pension-yojana"),
        ("PM Jan Dhan Yojana",            "General",            "Ministry of Finance",      "Zero-balance bank account",   "Unbanked citizens",                "pradhan-mantri-jan-dhan-yojana"),
        ("Scholarship for SC/ST",         "Education",          "Ministry of Social Justice","Full scholarship + stipend", "SC/ST students",                   "post-matric-scholarship-for-sc-students"),
        ("Kisan Credit Card",             "Agriculture",        "Ministry of Agriculture",  "Credit for farming needs",    "Farmers",                          "kisan-credit-card"),
        ("Soil Health Card",              "Agriculture",        "Ministry of Agriculture",  "Free soil testing",           "Farmers",                          "soil-health-card"),
        ("PM Rozgar Protsahan",           "Labour & Employment","Ministry of Labour",       "EPF contribution by govt",    "New hires",                        "pradhan-mantri-rojgar-protsahan-yojana"),
        ("Chiranjeevi Health Insurance",  "Health",             "Govt of Rajasthan",        "₹25 lakh cashless insurance", "Rajasthan residents",              "mukhyamantri-chiranjeevi-swasthya-bima-yojana"),
        ("Palanhar Yojana",               "Social Welfare",     "Govt of Rajasthan",        "₹2,500/month for orphans",    "Orphaned children of Rajasthan",   "palanhar-yojana"),
        ("Indira Rasoi Yojana",           "Food Security",      "Govt of Rajasthan",        "Meals at ₹8 per plate",       "Urban poor of Rajasthan",          "indira-rasoi-yojana"),
        ("Mukhyamantri Rajshri Yojana",   "Education",          "Govt of Rajasthan",        "₹50,000 for girl education",  "Girl child of Rajasthan",          "mukhyamantri-rajshri-yojana"),
        ("Lado Protsahan Yojana",         "Women & Child",      "Govt of Rajasthan",        "₹2 lakh savings bond",        "Girl child at birth, Rajasthan",   "lado-protsahan-yojana"),
    ]
    return [{
        "id": f"myscheme_{i+1}", 
        "scheme_name": n, 
        "category": c, 
        "ministry": m,
        "department": m,
        "application_link": f"{BASE_URL}/schemes/{slug}",
        "url": f"{BASE_URL}/schemes/{slug}", 
        "description": b,
        "benefits": b, 
        "eligibility": e,
        "launched": "", 
        "state": "Rajasthan", 
        "status": "Active",
        "source": "myscheme.gov.in (fallback)", 
        "source_mode": "fallback",
        "fetch_method": "curated_fallback",
        "source_note": "Curated Rajasthan-focused fallback dataset",
        "scraped_at": ts,
    } for i, (n, c, m, b, e, slug) in enumerate(schemes)]
