"""HaloClient against a mocked transport.

Two behaviours here are not obvious and were learned the hard way on the
sibling project against a live tenant. Both are pinned by tests so a future
tidy-up cannot quietly undo them.
"""
import httpx
import pytest

from src.halo_client import HaloClient


def client_with(handler) -> HaloClient:
    client = HaloClient(
        base_url="https://tenant.example",
        auth_url="https://tenant.example/auth/token",
        client_id="id",
        client_secret="secret",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def token_response():
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


def test_token_is_fetched_and_cached():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return token_response()

    client = client_with(handler)
    assert client.get_token() == "tok"
    assert client.get_token() == "tok"
    assert len(calls) == 1, "the token must be cached, not refetched per call"


def test_user_agent_is_sent_on_the_token_request_itself():
    """Halo's 2026 AWS/EKS migration made this mandatory. Omitting it fails
    in a way that looks like bad credentials, so it is asserted directly."""
    seen = {}

    def handler(request):
        seen[request.url.path] = request.headers.get("user-agent")
        return token_response()

    client = client_with(handler)
    client.get_token()
    assert seen["/auth/token"], "no User-Agent on the token request"
    assert "Halo-Cable-Label-Printer" in seen["/auth/token"]


def test_asset_list_requests_the_fields_array():
    """includeassetfields is omitted by default, and without it the `fields`
    array simply is not in the response."""
    seen = {}

    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"assets": [], "record_count": 0})

    client = client_with(handler)
    list(client.iter_assets())
    assert seen["includeassetfields"] == "true"


def test_pagination_counts_records_not_pages():
    """The live tenant silently caps the page size: ask for 200, get 50.
    Counting page_no * requested_size undercounts the pages needed and
    stops early, which looks exactly like 'the asset just isn't pending'."""
    pages = {
        1: {"assets": [{"id": i} for i in range(50)], "record_count": 120, "page_size": 50},
        2: {"assets": [{"id": i} for i in range(50, 100)], "record_count": 120, "page_size": 50},
        3: {"assets": [{"id": i} for i in range(100, 120)], "record_count": 120, "page_size": 50},
    }

    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        return httpx.Response(200, json=pages[int(request.url.params["page_no"])])

    client = client_with(handler)
    assert len(list(client.iter_assets(page_size=200))) == 120


def test_pagination_stops_on_an_empty_page():
    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        return httpx.Response(200, json={"assets": [], "record_count": 999})

    client = client_with(handler)
    assert list(client.iter_assets()) == []


def test_update_fields_posts_the_halo_envelope():
    """[{"id": asset, "fields": [...]}] -- the shape Halo expects, and the
    shape that lets three updates travel in one atomic call."""
    captured = {}

    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        captured["body"] = httpx.Request("POST", request.url, content=request.content).content
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={})

    client = client_with(handler)
    client.update_fields(4711, [{"id": 813, "value": "112"}, {"id": 811, "value": "0"}])

    assert captured["json"] == [
        {"id": 4711, "fields": [{"id": 813, "value": "112"}, {"id": 811, "value": "0"}]}
    ]


def test_read_counter_coerces_and_tolerates_junk():
    def make(value):
        def handler(request):
            if request.url.path == "/auth/token":
                return token_response()
            return httpx.Response(200, json={"id": 1, "fields": [{"id": 813, "value": value}]})

        return client_with(handler)

    assert make("112").read_counter(1, 813) == 112
    assert make(112).read_counter(1, 813) == 112
    assert make("").read_counter(1, 813) is None
    assert make("junk").read_counter(1, 813) is None


def test_read_counter_returns_none_for_a_missing_field():
    """None is treated as a mismatch by the allocator, which is correct:
    the check exists to refuse rendering on an uncertain counter."""
    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        return httpx.Response(200, json={"id": 1, "fields": []})

    assert client_with(handler).read_counter(1, 813) is None


def test_http_errors_propagate():
    """The allocator turns these into ReserveError; the client must not
    swallow them into a success."""
    def handler(request):
        if request.url.path == "/auth/token":
            return token_response()
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        client_with(handler).update_fields(1, [{"id": 813, "value": "1"}])
