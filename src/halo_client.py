"""OAuth2 client-credentials token management and asset read/write against
HaloITSM.

Ported from the sibling Halo-Asset-Tag-Printer, which earned two hard-won
facts against a live tenant. Both are load-bearing here:

1. **Zero visible assets is a PERMISSIONS problem, not a login-mode one.**
   An earlier note here (carried over from the sibling) claimed an
   application using "Application identity" could not resolve Asset
   visibility at all. Measured against the live tenant on 2026-09-18, that
   is wrong: a working integration and a broken one were BOTH using
   Application identity. The difference was what each could see -- 31 asset
   types versus 1. When a new application authenticates but lists no
   assets, compare its permissions and asset-type visibility against an
   integration that already works.

2. **There is no server-side filter-by-custom-field-value for Assets.**
   Unlike Tickets, there is no view_id and no query parameter that filters
   on a field's value. Callers list and filter client-side. Narrowing by
   asset type is the only cheap server-side reduction available.

Unlike the sibling, this client WRITES more than one field, and its
update path is the mechanism by which cable numbers are consumed. See
allocator.reserve() for why all three updates travel in one call.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import httpx

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_MARGIN_SECONDS = 60
DEFAULT_USER_AGENT = "Halo-Cable-Label-Printer/1.0 (+https://github.com/stnw11/Halo-Cable-Label-Printer)"
# The live tenant caps page_size at 50 whatever is requested, so asking for
# more only makes the request look bigger than the response. iter_assets()
# does not depend on this value being honoured.
DEFAULT_PAGE_SIZE = 50


class HaloClient:
    def __init__(
        self,
        base_url: str,
        auth_url: str,
        client_id: str,
        client_secret: str,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HaloClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _headers(self) -> dict:
        return {"User-Agent": self.user_agent, "Authorization": f"Bearer {self.get_token()}"}

    def get_token(self) -> str:
        """Fetch and cache a token, refreshing shortly before expiry.

        The User-Agent header goes on the TOKEN request too, not just the
        API calls after it -- Halo's 2026 AWS/EKS migration made that
        mandatory, and omitting it fails in a way that looks like bad
        credentials.
        """
        if self._token and time.time() < self._token_expires_at:
            return self._token

        logger.debug("fetching Halo token from %s", self.auth_url)
        response = self._client.post(
            self.auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "all",
            },
            headers={"User-Agent": self.user_agent},
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = payload.get("expires_in", 3600)
        self._token_expires_at = time.time() + expires_in - TOKEN_EXPIRY_MARGIN_SECONDS
        return self._token

    # --- reads ---------------------------------------------------------------

    def list_assets_page(
        self,
        page_no: int,
        page_size: int = DEFAULT_PAGE_SIZE,
        asset_group_id: int | None = None,
    ) -> dict:
        """One page of GET /api/Asset. includeassetfields=true is required to
        get the `fields` array back at all -- it is omitted by default.

        assetgroup_id is the service's only scope, and it is applied by Halo.
        Halo silently ignores query parameters it does not recognise rather
        than erroring, so a misspelled parameter would widen the scope to
        every visible asset without any error. This spelling was measured to
        work on the live tenant; tools/check_halo.py re-measures it.
        """
        params = {
            "pageinate": "true",
            "page_no": page_no,
            "page_size": page_size,
            "includeassetfields": "true",
        }
        if asset_group_id is not None:
            params["assetgroup_id"] = asset_group_id
        response = self._client.get(
            f"{self.base_url}/api/Asset", params=params, headers=self._headers()
        )
        response.raise_for_status()
        return response.json()

    def iter_assets(
        self,
        page_size: int = DEFAULT_PAGE_SIZE,
        asset_group_id: int | None = None,
    ) -> Iterator[dict]:
        """Yield every visible asset, paginating as needed.

        Tracks cumulative fetched count against `record_count` rather than
        page_no * page_size: the live tenant silently caps the effective
        page size (requesting 200 returns 50), so multiplying by the
        REQUESTED size undercounts the pages needed and stops early --
        which would look exactly like "the asset just isn't pending".
        """
        page_no = 1
        fetched = 0
        while True:
            page = self.list_assets_page(page_no, page_size, asset_group_id)
            assets = page.get("assets", [])
            yield from assets
            fetched += len(assets)
            record_count = page.get("record_count", fetched)
            if not assets or fetched >= record_count:
                return
            page_no += 1

    def get_asset(self, asset_id: int) -> dict:
        response = self._client.get(
            f"{self.base_url}/api/Asset/{asset_id}",
            params={"includeassetfields": "true"},
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def read_counter(self, asset_id: int, field_id: int) -> int | None:
        """Re-read Next Cable ID for the allocator's read-back check.

        Returns None if the field is missing or non-numeric, which the
        allocator treats as a mismatch -- the point of the check is to
        refuse to render on a counter whose state is not certain.
        """
        from .fields import as_int, field_by_id

        return as_int(field_by_id(self.get_asset(asset_id), field_id), default=None)

    # --- writes --------------------------------------------------------------

    def update_fields(self, asset_id: int, updates: list[dict]) -> None:
        """Write one or more custom fields on an asset in a single call.

        `updates` is [{"id": <field id>, "value": <value>}, ...].

        Halo enforces a field's read-only flag at the FORM layer, not the
        API layer, so the three service-owned fields (prefix, counter,
        status) can be view-only for agents and still written here. That is
        confirmed behavior, not an assumption.

        Sending all of a reservation's updates in one call is what makes the
        reserve atomic from Halo's point of view. Do not split this into
        several calls for convenience -- see allocator.reserve().
        """
        response = self._client.post(
            f"{self.base_url}/api/Asset",
            json=[{"id": asset_id, "fields": updates}],
            headers=self._headers(),
        )
        response.raise_for_status()

    def write_status(self, asset_id: int, field_id: int, line: str) -> None:
        """Write just the Last Label Run line. Used by reconcile.py for the
        terminal status, after the job has actually printed or failed."""
        self.update_fields(asset_id, [{"id": field_id, "value": line}])
