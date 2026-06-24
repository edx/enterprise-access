"""
Utility helpers for the API client implementation.
"""


def get_paginated_payloads(client, next_url):
    """
    Iterate through paginated API responses.

    Follows `next` links until all pages have been retrieved and yields
    the JSON payload from each page response.

    Args:
        client: HTTP client instance.
        next_url (str): Initial pagination URL.

    Yields:
        dict: Response payload for each page.
    """
    payloads = []

    while next_url:
        response = client.get(next_url)
        response.raise_for_status()

        payload = response.json()
        payloads.append(payload)

        next_url = payload.get("next")

    return payloads
