from __future__ import annotations

import os

from ..exceptions import ConfigurationError
from ._retry import with_retry

_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.rating,"
    "places.userRatingCount,places.priceLevel,places.googleMapsUri"
)
_PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def local_business_search(query: str, max_results: int = 5) -> str:
    """Search for local businesses (e.g. tradies like plumbers, electricians)
    and compare them by Google rating, review count, and price level.

    Uses the Google Places API (Text Search). Include the type of business
    and a location/area in the query, e.g. "licensed electrician in
    Parramatta NSW" or "emergency plumber near Bondi Beach".

    Args:
        query: What to search for, including the business type and a
            location or area, e.g. "plumber in Bondi NSW".
        max_results: Maximum number of results to return (default 5).

    Returns:
        Formatted string listing each business with its name, address,
        Google rating, number of reviews, and price level (if available).
    """
    try:
        import httpx
    except ImportError as exc:
        raise ConfigurationError(
            "local_business_search requires the 'httpx' package. "
            "Install it with: pip install 'bestteam[tools-places]'"
        ) from exc

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ConfigurationError(
            "local_business_search requires the GOOGLE_MAPS_API_KEY "
            "environment variable. Get a key at "
            "https://console.cloud.google.com/google/maps-apis and enable "
            "the Places API (New)."
        )

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    body = {"textQuery": query, "maxResultCount": max_results}

    class _ServerError(Exception):
        def __init__(self, response):
            self.response = response

    def _do_request():
        with httpx.Client(timeout=30) as client:
            response = client.post(_PLACES_URL, headers=headers, json=body)
        if response.status_code >= 500:
            raise _ServerError(response)
        return response

    try:
        response = with_retry(
            _do_request, retriable_exc=(_ServerError, httpx.RequestError)
        )
    except _ServerError as exc:
        response = exc.response
    except httpx.RequestError as exc:
        raise ConfigurationError(f"Places API request failed: {exc}") from exc

    if response.status_code != 200:
        raise ConfigurationError(
            f"Places API error [{response.status_code}]: {response.text}"
        )

    places = response.json().get("places", [])
    if not places:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, place in enumerate(places, 1):
        name = place.get("displayName", {}).get("text", "Unknown")
        address = place.get("formattedAddress", "")
        rating = place.get("rating")
        review_count = place.get("userRatingCount")
        price_level = _PRICE_LEVELS.get(place.get("priceLevel", ""), "")
        maps_uri = place.get("googleMapsUri", "")

        lines.append(f"{i}. {name}")
        if address:
            lines.append(f"   Address: {address}")
        if rating is not None:
            reviews = f" ({review_count} reviews)" if review_count is not None else ""
            lines.append(f"   Rating: {rating}/5{reviews}")
        if price_level:
            lines.append(f"   Price: {price_level}")
        if maps_uri:
            lines.append(f"   Maps: {maps_uri}")
        lines.append("")

    return "\n".join(lines)
