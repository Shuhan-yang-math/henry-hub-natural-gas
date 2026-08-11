import unittest

import numpy as np
import pandas as pd


from naturalgas.ncar_gdex_nonlinear_wind import (
    PowerCurveSpec,
    add_nonlinear_signals,
    aggregate_nonlinear_features,
    nonlinear_power_components,
)


class NonlinearWindTests(unittest.TestCase):
    def test_power_curve_has_low_middle_high_gas_shape(self):
        speed = np.array([2.0, 12.0, 20.0, 22.5, 25.0])
        values = nonlinear_power_components(speed)
        shortfall = values["total_shortfall_cf"]
        self.assertEqual(shortfall[0], 1.0)
        self.assertEqual(shortfall[1], 0.0)
        self.assertEqual(shortfall[2], 0.0)
        self.assertGreater(shortfall[3], 0.0)
        self.assertEqual(shortfall[4], 1.0)

    def test_shortfall_decomposition_is_exact(self):
        speed = np.linspace(0.0, 30.0, 121)
        values = nonlinear_power_components(speed)
        np.testing.assert_allclose(
            values["total_shortfall_cf"],
            values["low_wind_shortfall_cf"]
            + values["high_wind_cutout_loss_cf"],
            atol=1e-12,
        )

    def test_equal_derate_and_cutout_produces_hard_curve(self):
        values = nonlinear_power_components(
            np.array([24.9, 25.0]),
            spec=PowerCurveSpec(
                high_wind_derate_start_mps=25.0,
                cut_out_mps=25.0,
            ),
        )
        np.testing.assert_allclose(
            values["effective_power_cf"],
            np.array([1.0, 0.0]),
        )

    def test_curve_is_applied_before_aggregation(self):
        points = pd.DataFrame(
            {
                "forecast_reference_time_utc": pd.to_datetime(
                    ["2016-01-01T00:00Z"] * 2,
                    utc=True,
                ),
                "forecast_cycle_hour_utc": [0, 0],
                "location_id": ["a", "b"],
                "valid_time_utc": pd.to_datetime(
                    ["2016-01-02T00:00Z"] * 2,
                    utc=True,
                ),
                "wind_speed_80m_mps": [0.0, 12.0],
            }
        )
        result = aggregate_nonlinear_features(points)
        self.assertAlmostEqual(
            result["gfs_effective_power_cf_5d"].iloc[0],
            0.5,
        )
        mean_speed_curve = nonlinear_power_components(
            np.array([6.0])
        )["effective_power_cf"][0]
        self.assertNotAlmostEqual(mean_speed_curve, 0.5)

    def test_signal_standardization_uses_only_prior_rows(self):
        dates = pd.date_range(
            "2016-01-01",
            periods=5,
            tz="UTC",
        )
        frame = pd.DataFrame(
            {
                "forecast_reference_time_utc": dates,
                "forecast_cycle_hour_utc": [0] * 5,
                "gfs_total_wind_shortfall_cf_5d": [
                    1.0,
                    2.0,
                    3.0,
                    4.0,
                    100.0,
                ],
                "gfs_low_wind_shortfall_cf_5d": [
                    1.0,
                    2.0,
                    3.0,
                    4.0,
                    100.0,
                ],
                "gfs_high_wind_cutout_loss_cf_5d": [
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
            }
        )
        result = add_nonlinear_signals(
            frame,
            window=4,
            min_periods=2,
        )
        expected = (100.0 - 2.5) / pd.Series(
            [1.0, 2.0, 3.0, 4.0]
        ).std()
        self.assertAlmostEqual(
            result["sig_gdex_wind_nonlinear"].iloc[-1],
            expected,
        )


if __name__ == "__main__":
    unittest.main()
