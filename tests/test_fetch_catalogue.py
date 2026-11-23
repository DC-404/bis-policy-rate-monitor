"""Exercise catalogue discovery and errors against small, controlled HTTP responses."""

import httpx
import pytest

from bis_policy_rate_monitor.fetch import (
    BIS_CATALOGUE_URL,
    MAX_CATALOGUE_BYTES,
    FetchError,
    discover_archive,
    select_flat_zip_url,
)


def flat_link(href: str) -> str:
    """Render the product title in the nested markup used by the catalogue."""
    return f"""<a href="{href}">
        <article><h4>Central bank policy rates (CSV, flat)</h4>
        <time datetime="2026-11-19">19 Nov 2026</time></article>
    </a>"""


def test_discovery_uses_the_label_and_resolves_a_relative_url() -> None:
    """Select the flat archive among similarly named downloads and resolve its URL."""
    # Include other formats for the same flow and a flat archive for a different flow.
    html = """
        <a href="/standard.zip">Central bank policy rates (CSV)</a>
        <a href="/sdmx.zip">Central bank policy rates (SDMX2.1 Compact)</a>
        <a href="/exchange.zip">Bilateral exchange rates (CSV, flat)</a>
    """ + flat_link("/published/policy-rates-flat.zip")

    def catalogue_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == BIS_CATALOGUE_URL
        return httpx.Response(200, text=html)

    # Assert discovery through the mock HTTP boundary as well as the HTML parser.
    with httpx.Client(transport=httpx.MockTransport(catalogue_response)) as client:
        assert discover_archive(client) == "https://data.bis.org/published/policy-rates-flat.zip"


def test_missing_flat_link_has_a_clear_error() -> None:
    """Tell the caller which product is absent instead of selecting standard CSV."""
    # The catalogue contains a valid policy-rates link with the wrong representation.
    html = '<a href="/standard.zip">Central bank policy rates (CSV)</a>'
    with pytest.raises(FetchError, match=r"no 'Central bank policy rates \(CSV, flat\)' link"):
        select_flat_zip_url(html)


def test_different_targets_with_the_same_title_are_ambiguous() -> None:
    """Reject competing URLs instead of trusting their positions in the catalogue."""
    # Both links match the product label, but they point to different archives.
    with pytest.raises(FetchError, match="multiple"):
        select_flat_zip_url(flat_link("/first.zip") + flat_link("/second.zip"))


@pytest.mark.parametrize("href", ["http://data.bis.org/file.zip", "//example.org/file.zip", ""])
def test_flat_link_requires_a_download_on_the_catalogue_origin(href: str) -> None:
    """Reject catalogued targets that cannot serve as an HTTPS BIS archive."""
    # The cases cover insecure transport, a different host and an empty target.
    with pytest.raises(FetchError, match="HTTPS ZIP on BIS"):
        select_flat_zip_url(flat_link(href))


def test_catalogue_http_failure_has_a_clear_error() -> None:
    """Expose the HTTP status when the catalogue request itself fails."""
    # Use a 503 response to exercise the request error path before HTML parsing.
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(FetchError, match="HTTP 503"):
            discover_archive(client)


def test_oversized_catalogue_has_a_clear_error() -> None:
    """Bound the catalogue body before attempting to parse its markup."""
    # Serve one byte beyond the permitted catalogue size.
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (MAX_CATALOGUE_BYTES + 1))
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(FetchError, match="exceeds the 4 MiB size limit"):
            discover_archive(client)
