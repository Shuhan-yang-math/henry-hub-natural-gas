from __future__ import annotations

import pytest

from naturalgas import ncar_gdex_bulk_wind_backfill_to_gcs as bulk_wind
from naturalgas import ncar_gdex_wind_backfill_to_gcs as wind
from naturalgas import open_meteo_us_ng_backfill as regional_weather
from naturalgas import (
    open_meteo_us_production_freezeoff_backfill as production_weather,
)


@pytest.mark.parametrize(
    "entrypoint,required_flag",
    [
        (wind.main, "--execute"),
        (bulk_wind.main, "--execute"),
        (regional_weather.main, "--upload"),
        (production_weather.main, "--upload"),
    ],
)
def test_remote_backfills_require_explicit_authorization(
    entrypoint,
    required_flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        entrypoint([])
    assert caught.value.code == 2
    assert required_flag in capsys.readouterr().err
