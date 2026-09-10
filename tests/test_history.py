"""History requests must stay inside EG.D's rolling three-year window."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from custom_components.ha_egd_openapi import api
from custom_components.ha_egd_openapi import coordinator as coordinator_module
from custom_components.ha_egd_openapi import statistics as statistics_module
from custom_components.ha_egd_openapi.coordinator import EgdDataUpdateCoordinator


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("2026-09-10T16:02:00+00:00", "2023-09-11T16:15:00+00:00"),
        ("2026-09-10T16:15:00+00:00", "2023-09-11T16:15:00+00:00"),
        ("2026-09-10T16:15:00.000001+00:00", "2023-09-11T16:30:00+00:00"),
        ("2026-09-10T23:59:59+00:00", "2023-09-12T00:00:00+00:00"),
        ("2024-02-29T12:02:00+00:00", "2021-03-01T12:15:00+00:00"),
        ("2026-09-11T00:02:00+02:00", "2023-09-11T22:15:00+00:00"),
    ],
)
def test_history_start_rounds_forward_in_utc(reference, expected) -> None:
    assert api.get_history_start(
        datetime.fromisoformat(reference)
    ) == datetime.fromisoformat(expected)


@pytest.mark.parametrize("method", ["async_probe_access", "async_get_profile_data"])
@pytest.mark.parametrize(
    "start,end,expected_start",
    [
        ("2023-09-10T00:00:00", "2023-09-10T23:45:00", None),
        ("2023-09-11T00:00:00", "2023-09-11T23:45:00", "2023-09-11T16:15:00"),
        ("2026-08-01T00:00:00", "2026-08-01T23:45:00", "2026-08-01T00:00:00"),
    ],
)
@pytest.mark.asyncio
async def test_probe_and_download_use_same_history_start(
    freeze_time, method, start, end, expected_start
) -> None:
    freeze_time(datetime(2026, 9, 10, 16, 2, tzinfo=timezone.utc))
    client = api.EgdApiClient(MagicMock(), "fake-id", "fake-secret")
    client._async_request_profile_data = AsyncMock(
        return_value=(200, {"data": [], "total": 0})
    )
    result = await getattr(client, method)(
        ean="859182400000000000",
        profile="DCQC",
        from_dt=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        to_dt=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
    )
    if expected_start is None:
        client._async_request_profile_data.assert_not_awaited()
    else:
        client._async_request_profile_data.assert_awaited_once()
        request = client._async_request_profile_data.call_args.kwargs
        assert request["from_dt"] == datetime.fromisoformat(expected_start).replace(
            tzinfo=timezone.utc
        )
    assert result == (
        expected_start is not None if method == "async_probe_access" else []
    )


@pytest.mark.parametrize(
    "account_start", ["2023-09-11T16:15:00", "2026-08-01T00:00:00"]
)
@pytest.mark.asyncio
async def test_initial_sync_respects_history_boundary(
    freeze_time, monkeypatch, account_start
) -> None:
    """Reproduce the log's first probe, then import through the real API client."""
    now = datetime(2026, 9, 10, 16, 2, tzinfo=timezone.utc)
    freeze_time(now)
    safe_start = datetime(2023, 9, 11, 16, 15, tzinfo=timezone.utc)
    account_start = datetime.fromisoformat(account_start).replace(tzinfo=timezone.utc)
    reading_time = datetime(2026, 9, 9, 20, tzinfo=timezone.utc)
    coordinator = EgdDataUpdateCoordinator.__new__(EgdDataUpdateCoordinator)
    coordinator.hass = MagicMock()
    coordinator._persisted = {}
    coordinator._store = SimpleNamespace(async_save=AsyncMock())
    coordinator.config_entry = SimpleNamespace(
        title="Fake C1 meter",
        options={},
        data={
            "ean": "859182400000000000",
            "import_profile": "DCQC",
            "export_profile": "DSQC",
            "update_hour": 16,
            "update_minute": 17,
        },
    )
    coordinator.client = api.EgdApiClient(MagicMock(), "fake-id", "fake-secret")

    async def respond(**request):
        if request["from_dt"] < now.replace(year=now.year - 3):
            return 400, {
                "error": "validation_error",
                "message": 'Datum "from" nesmí být starší než 3 roky.',
            }
        if request["from_dt"] < account_start:
            return 400, {
                "error": "validation_error",
                "message": "Nemáte oprávnění na data odběrného místa.",
            }
        records = []
        if request["from_dt"] <= reading_time <= request["to_dt"]:
            records.append(
                {
                    "timestamp": reading_time.isoformat(),
                    "value": 0.25 if request["profile"] == "DCQC" else 0.05,
                    "status": "W",
                }
            )
        return 200, {
            "profile": request["profile"],
            "units": "kWh",
            "data": records,
            "total": len(records),
        }

    coordinator.client._async_request_profile_data = AsyncMock(side_effect=respond)
    add_statistics = MagicMock()
    monkeypatch.setattr(
        statistics_module, "async_add_external_statistics", add_statistics
    )
    state = await coordinator._async_refresh_energy_state()

    assert state.total_import_kwh == 0.25
    assert state.total_export_kwh == 0.05
    assert add_statistics.call_count == 2
    requests = coordinator.client._async_request_profile_data.call_args_list
    assert requests[0].kwargs["from_dt"] == safe_start
    for call in requests:
        start = call.kwargs["from_dt"]
        assert start >= safe_start
        assert start.minute % 15 == start.second == start.microsecond == 0
    for direction in ("import", "export"):
        assert coordinator._persisted[
            f"{direction}_accessible_start"
        ] == account_start.isoformat().replace("+00:00", "Z")
    coordinator._store.async_save.assert_awaited_once()


@pytest.fixture
def freeze_time(monkeypatch):
    def freeze(now):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return (
                    now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
                )

        monkeypatch.setattr(api, "datetime", FrozenDateTime)
        monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: now)
        monkeypatch.setattr(
            coordinator_module.dt_util,
            "now",
            lambda: now.astimezone(ZoneInfo("Europe/Prague")),
        )

    return freeze
