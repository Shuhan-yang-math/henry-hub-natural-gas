from datetime import date, datetime, timezone
import unittest

import numpy as np
import pandas as pd


from naturalgas.ncar_gdex_wind_backfill_to_gcs import (
    DEFAULT_LEAD_DAYS,
    DEFAULT_VALID_HOURS,
    ForecastTask,
    forecast_tasks,
    make_daily,
    make_features,
    month_ranges,
)


class NcarGdexWindBackfillTests(unittest.TestCase):
    def test_forecast_tasks_preserve_init_lead_and_valid_time(self):
        tasks = forecast_tasks(
            date(2016, 1, 1),
            date(2016, 1, 1),
            (1, 2),
            (0, 6),
        )
        self.assertEqual(len(tasks), 4)
        self.assertEqual(tasks[0].forecast_lead_hours, 24)
        self.assertEqual(
            tasks[0].valid_time_utc,
            datetime(2016, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(tasks[-1].forecast_lead_hours, 54)
        self.assertEqual(
            tasks[-1].source_filename,
            "gfs.0p25.2016010100.f054.grib2",
        )

    def test_month_ranges_clip_edges(self):
        self.assertEqual(
            month_ranges(date(2016, 1, 15), date(2016, 3, 2)),
            [
                (date(2016, 1, 15), date(2016, 1, 31)),
                (date(2016, 2, 1), date(2016, 2, 29)),
                (date(2016, 3, 1), date(2016, 3, 2)),
            ],
        )

    def test_daily_and_feature_aggregation(self):
        rows = []
        for issue_day in (1, 2):
            initialization = datetime(
                2016, 1, issue_day, tzinfo=timezone.utc
            )
            for lead_day in DEFAULT_LEAD_DAYS:
                target = initialization.date() + pd.Timedelta(days=lead_day)
                for location_id in ("a", "b"):
                    for valid_hour in DEFAULT_VALID_HOURS:
                        rows.append(
                            {
                                "dataset_id": "d084001",
                                "model": "ncep_gfs_0p25",
                                "forecast_reference_time_utc": initialization,
                                "target_date": target,
                                "lead_days": lead_day,
                                "location_id": location_id,
                                "city": location_id,
                                "state": "TX",
                                "census_division": "West South Central",
                                "requested_latitude": 30.0,
                                "requested_longitude": -100.0,
                                "grid_latitude": 30.0,
                                "grid_longitude": -100.0,
                                "wind_speed_80m_mps": np.float32(
                                    lead_day + valid_hour / 24
                                ),
                                "u_wind_80m_mps": np.float32(1.0),
                                "v_wind_80m_mps": np.float32(1.0),
                            }
                        )
        daily = make_daily(pd.DataFrame(rows), expected_samples=4)
        self.assertEqual(len(daily), 2 * 5 * 2)
        city_leads, features = make_features(
            daily,
            expected_lead_days=5,
            expected_locations=2,
        )
        self.assertEqual(len(city_leads), 2 * 5)
        self.assertEqual(len(features), 2)
        self.assertTrue(features["gfs_lead_count"].eq(5).all())

    def test_forecast_task_source_path(self):
        task = ForecastTask(
            initialization_time_utc=datetime(
                2024, 1, 20, tzinfo=timezone.utc
            ),
            lead_days=1,
            forecast_lead_hours=24,
        )
        self.assertEqual(
            task.source_path,
            "files/g/d084001/2024/20240120/"
            "gfs.0p25.2024012000.f024.grib2",
        )

    def test_features_can_explicitly_retain_partial_issue_time(self):
        rows = []
        initialization = datetime(2018, 5, 26, 18, tzinfo=timezone.utc)
        for lead_day in (3, 4, 5):
            target = initialization.date() + pd.Timedelta(days=lead_day)
            for location_id in ("a", "b"):
                rows.append(
                    {
                        "dataset_id": "d084001",
                        "model": "ncep_gfs_0p25",
                        "forecast_reference_time_utc": initialization,
                        "target_date": target,
                        "lead_days": lead_day,
                        "location_id": location_id,
                        "city": location_id,
                        "state": "TX",
                        "census_division": "West South Central",
                        "requested_latitude": 30.0,
                        "requested_longitude": -100.0,
                        "grid_latitude": 30.0,
                        "grid_longitude": -100.0,
                        "wind_sample_count": 4,
                        "wind_sample_complete": True,
                        "wind_speed_80m_mean_kmh": 20.0,
                        "nominal_issue_date": initialization.date(),
                    }
                )
        daily = pd.DataFrame(rows)
        with self.assertRaisesRegex(Exception, "all requested lead days"):
            make_features(
                daily,
                expected_lead_days=5,
                expected_locations=2,
            )
        _, features = make_features(
            daily,
            expected_lead_days=5,
            expected_locations=2,
            require_complete=False,
        )
        self.assertEqual(len(features), 1)
        self.assertEqual(int(features.iloc[0]["gfs_lead_count"]), 3)


if __name__ == "__main__":
    unittest.main()
