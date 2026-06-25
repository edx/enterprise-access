"""
Utility helpers for the API client implementation.
"""


def fetch_all_results(client, url, params=None):
    """
    Fetch all paginated results.

    Args:
        client: HTTP client.
        url (str): Endpoint URL.
        params (dict | None): Optional query parameters.

    Returns:
        dict: Response payload with all pages merged.
    """
    if params is None:
        params = {}

    response = client.get(url, params=params)
    response.raise_for_status()

    data = response.json()

    while data.get("next"):
        response = client.get(data["next"])
        response.raise_for_status()

        page = response.json()

        data["results"].extend(page.get("results", []))
        data["next"] = page.get("next")
        data["previous"] = data.get("previous") or page.get("previous")

    return data
