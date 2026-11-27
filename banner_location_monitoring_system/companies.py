"""
companies.py - registry of monitored companies.

Each entry points at a scraper module in surya_banner/ that exposes
`scrape() -> pandas.DataFrame`. The registry is synced into the
blms_companies table on every run; switch a company off by setting
is_active = false in Supabase (the monitor skips it) - no code change needed.

Fields
  key              stable identifier (never rename - used as the DB key)
  id_field         scraper column holding the official store ID, or None.
                   The store-page URL is used where a site has no numeric ID
                   (the slug is stable per store).
  min_locations    a scrape returning fewer rows than this is treated as
                   incomplete (blocked / layout change) and nothing is removed.
  needs_browser    True = patchright + a display (xvfb-run in CI).

To add a company: drop a scraper with a scrape() function into surya_banner/
and add one entry below.
"""

COMPANIES = [
    {
        "key": "best_friends_pet_care", "company_name": "Best Friends Pet Care",
        "module": "best_pet_care", "website_url": "https://bestfriendspetcare.com",
        "locator_url": "https://bestfriendspetcare.com/locations/",
        "scraper_type": "API", "id_field": "Store ID", "min_locations": 50,
    },
    {
        "key": "chuck_and_dons", "company_name": "Chuck & Don's",
        "module": "chunkanddont", "website_url": "https://www.chuckanddons.com",
        "locator_url": "https://stores.chuckanddons.com/",
        "scraper_type": "JSON (Next.js)", "id_field": "Store Code", "min_locations": 40,
    },
    {
        "key": "doggie_style_pets", "company_name": "Doggie Style Pets",
        "module": "doggie_style", "website_url": "https://www.doggiestylepets.com",
        "locator_url": "https://www.doggiestylepets.com/pages/find-a-store",
        "scraper_type": "HTML", "id_field": None, "min_locations": 6,
    },
    {
        "key": "dogtopia", "company_name": "Dogtopia",
        "module": "dogtopia", "website_url": "https://www.dogtopia.com",
        "locator_url": "https://www.dogtopia.com/location-finder/store-listing/",
        "scraper_type": "HTML", "id_field": "Store Page", "min_locations": 180,
    },
    {
        "key": "earthwise_pet", "company_name": "EarthWise Pet",
        "module": "earthwisepet", "website_url": "https://earthwisepet.com",
        "locator_url": "https://earthwisepet.com/pet-stores-near-me",
        "scraper_type": "JSON (hydration)", "id_field": "Store ID", "min_locations": 100,
    },
    {
        "key": "feeders_pet_supply", "company_name": "Feeders Pet Supply",
        "module": "Feeder", "website_url": "https://www.feederspetsupply.com",
        "locator_url": "https://www.feederspetsupply.com/pages/store-locator",
        "scraper_type": "HTML", "id_field": "Store Page", "min_locations": 35,
    },
    {
        "key": "friendly_pets", "company_name": "Friendly Pets",
        "module": "friendlystore", "website_url": "https://friendlypets.com",
        "locator_url": "https://friendlypets.com/locations",
        "scraper_type": "HTML", "id_field": None, "min_locations": 1,
    },
    {
        "key": "hollywood_feed", "company_name": "Hollywood Feed",
        "module": "hollywood", "website_url": "https://www.hollywoodfeed.com",
        "locator_url": "https://locations.hollywoodfeed.com/",
        "scraper_type": "Browser (Cloudflare)", "id_field": "Store ID", "min_locations": 120,
        "needs_browser": True,
    },
    {
        "key": "kahoots", "company_name": "Kahoots Feed & Pet",
        "module": "kahoots", "website_url": "https://kahootsfeedandpet.com",
        "locator_url": "https://kahootsfeedandpet.com/pages/locations",
        "scraper_type": "HTML", "id_field": "Store Page", "min_locations": 15,
    },
    {
        "key": "krisers", "company_name": "Kriser's Natural Pet",
        "module": "kriser", "website_url": "https://www.krisers.com",
        "locator_url": "https://stores.krisers.com/",
        "scraper_type": "JSON (Next.js)", "id_field": "Store Code", "min_locations": 12,
    },
    {
        "key": "mud_bay", "company_name": "Mud Bay",
        "module": "mudbay", "website_url": "https://www.mudbay.com",
        "locator_url": "https://www.mudbay.com/storefinder",
        "scraper_type": "Browser (Cloudflare)", "id_field": "Store ID", "min_locations": 40,
        "needs_browser": True,
    },
]

# Default monitoring cadence written to blms_companies.scrape_frequency.
DEFAULT_FREQUENCY = "weekly"
