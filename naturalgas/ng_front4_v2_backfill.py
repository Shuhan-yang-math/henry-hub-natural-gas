"""Backfill daily Henry Hub NG C1-C4 prices from the NYMEX v2 trade bucket.

The EIA input contains official daily settlements through 2024-04-05.  The v2
source contains tick trades, not settlements, so this script derives a clearly
labelled proxy: trade-volume-weighted price between 14:28 and 14:30 New York
time for each of the first four unexpired outright NG contracts.

The raw EIA object is never overwritten.  Combined and audit-friendly outputs
are written under processed/ng_hh_futures/.
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import gcsfs
import numpy as np
import pandas as pd
import pyarrow.parquet as pq


SOURCE_BUCKET = "level-array-501205-r2-futures-prices-v2"
TARGET_BUCKET = "bcli-natgas-data-497807"
EIA_KEY = f"{TARGET_BUCKET}/raw/eia/prices/nymex_futures_front4_daily.csv"
TRADES_PREFIX = f"{SOURCE_BUCKET}/exchange=NYMEX/trades"
OUTPUT_PREFIX = f"{TARGET_BUCKET}/processed/ng_hh_futures"
LOCAL_OUTPUT_DIR = Path(__file__).resolve().parent / "processed" / "ng_front4_v2"

OUTRIGHT_RE = re.compile(r"^NG([FGHJKMNQUVXZ])(\d{1,2})$")
FILE_DATE_RE = re.compile(r"trades_NYMEX_(\d{4}-\d{2}-\d{2})\.parquet$")
MONTH_BY_CODE = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}

PRICE_TYPE = "settlement_window_vwap_proxy"
NORMAL_METHOD = "outright_trade_vwap_14:28:00_14:29:59.999_America/New_York"
EXPIRY_METHOD = "expiring_outright_trade_vwap_14:00:00_14:29:59.999_America/New_York"
FALLBACK_METHOD = "outright_trade_vwap_14:00:00_14:29:59.999_America/New_York_fallback"
EARLY_CLOSE_METHOD = (
    "early_close_outright_trade_vwap_13:28:00_13:29:59.999_America/New_York"
)
SOURCE_GAP_METHOD = (
    "source_gap_preceding_outright_trade_vwap_13:58:00_13:59:59.999_"
    "America/New_York"
)


@dataclass(frozen=True)
class TradeFile:
    trading_date: pd.Timestamp
    key: str


def _stat_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _ng_row_groups(parquet_file: pq.ParquetFile) -> list[int]:
    """Use contract-symbol min/max statistics to avoid reading other products."""
    symbol_idx = parquet_file.schema_arrow.names.index("contract_symbol")
    groups: list[int] = []
    for i in range(parquet_file.metadata.num_row_groups):
        stats = parquet_file.metadata.row_group(i).column(symbol_idx).statistics
        if stats is None or not stats.has_min_max:
            groups.append(i)
            continue
        minimum = _stat_text(stats.min)
        maximum = _stat_text(stats.max)
        if maximum >= "NG" and minimum < "NH":
            groups.append(i)
    return groups


def _delivery_month(symbol: str, trading_date: pd.Timestamp) -> pd.Timestamp | None:
    match = OUTRIGHT_RE.fullmatch(symbol)
    if not match:
        return None
    month_code, year_code = match.groups()
    month = MONTH_BY_CODE[month_code]

    if len(year_code) == 2:
        year = 2000 + int(year_code)
        candidate = pd.Timestamp(year=year, month=month, day=1)
        return candidate if candidate > trading_date.normalize() else None

    digit = int(year_code)
    candidates = [
        pd.Timestamp(year=year, month=month, day=1)
        for year in range(trading_date.year - 1, trading_date.year + 11)
        if year % 10 == digit
    ]
    future = [candidate for candidate in candidates if candidate > trading_date.normalize()]
    return min(future) if future else None


def extract_trade_file(trade_file: TradeFile, fs: gcsfs.GCSFileSystem) -> list[dict]:
    """Return up to four C1-C4 proxy records from one NYMEX daily trade file."""
    with fs.open(trade_file.key, "rb") as handle:
        parquet_file = pq.ParquetFile(handle)
        groups = _ng_row_groups(parquet_file)
        if not groups:
            return []
        table = parquet_file.read_row_groups(
            groups,
            columns=[
                "contract_symbol",
                "event_timestamp",
                "last",
                "last_volume",
                "source_event_type",
            ],
        )

    frame = table.to_pandas()
    if frame.empty:
        return []
    frame = frame.loc[
        frame["contract_symbol"].str.fullmatch(OUTRIGHT_RE, na=False)
        & frame["last"].gt(0)
        & frame["last_volume"].gt(0)
        & frame["source_event_type"].eq("trade")
    ].copy()
    if frame.empty:
        return []

    frame["event_timestamp"] = pd.to_datetime(frame["event_timestamp"], utc=True)
    frame["event_timestamp_et"] = frame["event_timestamp"].dt.tz_convert(
        "America/New_York"
    )
    minute = (
        frame["event_timestamp_et"].dt.hour * 60
        + frame["event_timestamp_et"].dt.minute
    )
    frame = frame.loc[
        frame["event_timestamp_et"].dt.date.eq(trade_file.trading_date.date())
    ].copy()
    if frame.empty:
        return []

    minute = minute.loc[frame.index]
    normal_period = minute.ge(14 * 60) & minute.lt(14 * 60 + 30)
    day_last_minute = int(minute.max())
    if normal_period.any():
        base_start = 14 * 60
        normal_start = 14 * 60 + 28
        period_end = 14 * 60 + 30
        window_kind = "normal"
    elif day_last_minute < 14 * 60 + 30:
        # Christmas Eve energy settlement is one hour earlier.  Requiring the
        # entire file to end early prevents a source outage from being mistaken
        # for an exchange early close.
        base_start = 13 * 60
        normal_start = 13 * 60 + 28
        period_end = 13 * 60 + 30
        window_kind = "early_close"
    else:
        # A known v2 source outage on 2025-02-13 spans the normal settlement
        # window.  Use only information available before the outage, never a
        # later/future trade, and label it separately.
        base_start = 13 * 60 + 30
        normal_start = 13 * 60 + 58
        period_end = 14 * 60
        window_kind = "source_gap"

    frame = frame.loc[minute.ge(base_start) & minute.lt(period_end)].copy()
    if frame.empty:
        return []
    minute = minute.loc[frame.index]

    frame["delivery_month"] = frame["contract_symbol"].map(
        lambda symbol: _delivery_month(symbol, trade_file.trading_date)
    )
    frame = frame.dropna(subset=["delivery_month"])
    if frame.empty:
        return []

    frame["in_normal_window"] = minute.ge(normal_start)
    frame["notional_30m"] = frame["last"] * frame["last_volume"]
    frame["volume_2m"] = frame["last_volume"].where(frame["in_normal_window"], 0)
    frame["notional_2m"] = frame["notional_30m"].where(
        frame["in_normal_window"], 0.0
    )
    frame["trade_count_2m"] = frame["in_normal_window"].astype(int)
    aggregated = (
        frame.groupby(["delivery_month", "contract_symbol"], as_index=False)
        .agg(
            notional_30m=("notional_30m", "sum"),
            volume_30m=("last_volume", "sum"),
            trade_count_30m=("last", "size"),
            notional_2m=("notional_2m", "sum"),
            volume_2m=("volume_2m", "sum"),
            trade_count_2m=("trade_count_2m", "sum"),
            first_event_timestamp_utc=("event_timestamp", "min"),
            last_event_timestamp_utc=("event_timestamp", "max"),
        )
        .assign(
            price_30m=lambda x: x["notional_30m"] / x["volume_30m"],
            price_2m=lambda x: x["notional_2m"] / x["volume_2m"].replace(0, np.nan),
        )
    )

    # Defensive handling for duplicate symbol encodings of the same delivery month:
    # retain the representation with the highest settlement-window volume.
    aggregated = (
        aggregated.sort_values(
            ["delivery_month", "volume_30m", "trade_count_30m"],
            ascending=[True, False, False],
        )
        .drop_duplicates("delivery_month", keep="first")
        .sort_values("delivery_month")
        .head(4)
        .reset_index(drop=True)
    )

    rows: list[dict] = []
    for rank, row in aggregated.iterrows():
        rows.append(
            {
                "date": trade_file.trading_date,
                "contract_rank": rank + 1,
                "contract_symbol": row["contract_symbol"],
                "delivery_month": row["delivery_month"],
                "price_2m_raw": float(row["price_2m"]),
                "price_30m_raw": float(row["price_30m"]),
                "trade_count_2m": int(row["trade_count_2m"]),
                "volume_2m": int(row["volume_2m"]),
                "trade_count_30m": int(row["trade_count_30m"]),
                "volume_30m": int(row["volume_30m"]),
                "first_event_timestamp_utc": row["first_event_timestamp_utc"],
                "last_event_timestamp_utc": row["last_event_timestamp_utc"],
                "source": "level-array-501205-r2-futures-prices-v2",
                "price_type": PRICE_TYPE,
                "window_kind": window_kind,
            }
        )
    return rows


def list_trade_files(
    fs: gcsfs.GCSFileSystem, start_date: pd.Timestamp
) -> list[TradeFile]:
    keys = fs.glob(f"{TRADES_PREFIX}/trades_NYMEX_*.parquet")
    files: list[TradeFile] = []
    for key in keys:
        match = FILE_DATE_RE.search(key)
        if not match:
            continue
        trading_date = pd.Timestamp(match.group(1))
        if trading_date >= start_date:
            files.append(TradeFile(trading_date=trading_date, key=key))
    return sorted(files, key=lambda item: item.trading_date)


def load_eia(fs: gcsfs.GCSFileSystem) -> tuple[pd.DataFrame, pd.DataFrame]:
    with fs.open(EIA_KEY, "rb") as handle:
        raw = pd.read_csv(handle)
    raw["period"] = pd.to_datetime(raw["period"])
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")
    raw = raw.loc[raw["series"].isin(["RNGC1", "RNGC2", "RNGC3", "RNGC4"])]
    raw = raw.drop_duplicates(["period", "series"], keep="last")
    wide = (
        raw.pivot(index="period", columns="series", values="value")
        .rename(
            columns={
                "RNGC1": "c1",
                "RNGC2": "c2",
                "RNGC3": "c3",
                "RNGC4": "c4",
            }
        )
        .sort_index()
    )
    wide.columns.name = None
    return raw, wide


def build_outputs(
    eia_raw: pd.DataFrame,
    eia_wide: pd.DataFrame,
    derived: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    derived = derived.sort_values(["date", "contract_rank"]).copy()
    front = derived.loc[derived["contract_rank"].eq(1), ["date", "contract_symbol"]]
    front = front.sort_values("date")
    front["is_expiration_day"] = (
        front["contract_symbol"].ne(front["contract_symbol"].shift(-1))
        & front["date"].lt(front["date"].max())
    )
    derived = derived.merge(
        front[["date", "is_expiration_day"]], on="date", how="left"
    )
    derived["is_expiration_contract"] = (
        derived["contract_rank"].eq(1) & derived["is_expiration_day"]
    )
    derived["price_raw"] = np.where(
        derived["is_expiration_contract"],
        derived["price_30m_raw"],
        derived["price_2m_raw"],
    )
    derived["method"] = np.where(
        derived["is_expiration_contract"], EXPIRY_METHOD, NORMAL_METHOD
    )
    derived.loc[derived["window_kind"].eq("early_close"), "method"] = (
        EARLY_CLOSE_METHOD
    )
    derived.loc[derived["window_kind"].eq("source_gap"), "method"] = (
        SOURCE_GAP_METHOD
    )
    fallback = derived["price_raw"].isna() & derived["price_30m_raw"].notna()
    derived.loc[fallback, "price_raw"] = derived.loc[fallback, "price_30m_raw"]
    derived.loc[fallback, "method"] = FALLBACK_METHOD
    derived["price"] = derived["price_raw"].round(3)
    uses_30m_statistics = derived["is_expiration_contract"] | fallback
    derived["trade_count"] = np.where(
        uses_30m_statistics,
        derived["trade_count_30m"],
        derived["trade_count_2m"],
    ).astype(int)
    derived["volume"] = np.where(
        uses_30m_statistics,
        derived["volume_30m"],
        derived["volume_2m"],
    ).astype(int)

    complete_dates = (
        derived.dropna(subset=["price"])
        .groupby("date")["contract_rank"]
        .nunique()
        .loc[lambda x: x == 4]
        .index
    )
    derived_complete = derived.loc[derived["date"].isin(complete_dates)].copy()

    cutoff = eia_wide[["c1", "c2", "c3", "c4"]].dropna(how="all").index.max()
    backfill = derived_complete.loc[derived_complete["date"] > cutoff].copy()

    v2_wide = (
        derived_complete.pivot(index="date", columns="contract_rank", values="price")
        .rename(columns={1: "c1", 2: "c2", 3: "c3", 4: "c4"})
        .sort_index()
    )
    symbol_wide = (
        derived_complete.pivot(
            index="date", columns="contract_rank", values="contract_symbol"
        )
        .rename(
            columns={
                1: "c1_contract_symbol",
                2: "c2_contract_symbol",
                3: "c3_contract_symbol",
                4: "c4_contract_symbol",
            }
        )
        .sort_index()
    )
    method_wide = (
        derived_complete.pivot(index="date", columns="contract_rank", values="method")
        .rename(
            columns={
                1: "c1_method",
                2: "c2_method",
                3: "c3_method",
                4: "c4_method",
            }
        )
        .sort_index()
    )
    window_kind = (
        derived_complete.groupby("date")["window_kind"].first().rename("window_kind")
    )
    v2_wide = v2_wide.join(symbol_wide).join(method_wide).join(window_kind)
    v2_wide["source"] = "v2_nymex_trades"
    v2_wide["price_type"] = PRICE_TYPE

    eia_panel = eia_wide.copy()
    eia_panel["source"] = "eia_official_settlement"
    eia_panel["price_type"] = "official_settlement"
    for column in [
        "c1_contract_symbol",
        "c2_contract_symbol",
        "c3_contract_symbol",
        "c4_contract_symbol",
        "c1_method",
        "c2_method",
        "c3_method",
        "c4_method",
    ]:
        eia_panel[column] = pd.NA
    eia_panel["window_kind"] = "official_eia"

    combined_panel = pd.concat(
        [eia_panel.loc[eia_panel.index <= cutoff], v2_wide.loc[v2_wide.index > cutoff]]
    ).sort_index()
    combined_panel = combined_panel.reset_index(names="date")

    metadata_columns = [
        "data_source",
        "price_type",
        "contract_symbol",
        "delivery_month",
        "method",
    ]
    eia_long = eia_raw.copy()
    eia_long["period"] = eia_long["period"].dt.strftime("%Y-%m-%d")
    eia_long["data_source"] = "EIA"
    eia_long["price_type"] = "official_settlement"
    eia_long["contract_symbol"] = pd.NA
    eia_long["delivery_month"] = pd.NaT
    eia_long["method"] = "published_EIA_value"

    rows = []
    for record in backfill.to_dict("records"):
        rank = int(record["contract_rank"])
        rows.append(
            {
                "period": pd.Timestamp(record["date"]).strftime("%Y-%m-%d"),
                "duoarea": "Y35NY",
                "area-name": "NEW YORK CITY",
                "product": "EPG0",
                "product-name": "Natural Gas",
                "process": f"PE{rank}",
                "process-name": f"Future Contract {rank}",
                "series": f"RNGC{rank}",
                "series-description": (
                    f"Natural Gas Futures Contract {rank} "
                    "(Dollars per Million Btu)"
                ),
                "value": record["price"],
                "units": "$/MMBTU",
                "data_source": "v2_nymex_trades",
                "price_type": PRICE_TYPE,
                "contract_symbol": record["contract_symbol"],
                "delivery_month": record["delivery_month"],
                "method": record["method"],
            }
        )
    backfill_long = pd.DataFrame(rows)
    for column in metadata_columns:
        if column not in eia_long:
            eia_long[column] = pd.NA
    combined_long = pd.concat(
        [eia_long, backfill_long],
        ignore_index=True,
        sort=False,
    ).sort_values(["period", "series"])

    return derived_complete, combined_panel, combined_long


def validation_report(
    eia_wide: pd.DataFrame, derived_complete: pd.DataFrame
) -> tuple[pd.DataFrame, dict]:
    v2 = (
        derived_complete.pivot(index="date", columns="contract_rank", values="price")
        .rename(columns={1: "c1", 2: "c2", 3: "c3", 4: "c4"})
        .sort_index()
    )
    overlap = eia_wide.join(v2, how="inner", lsuffix="_eia", rsuffix="_v2")
    metrics = {}
    for rank in range(1, 5):
        eia_col, v2_col = f"c{rank}_eia", f"c{rank}_v2"
        valid = overlap[[eia_col, v2_col]].dropna()
        difference = valid[v2_col] - valid[eia_col]
        metrics[f"c{rank}"] = {
            "observations": int(len(valid)),
            "exact_to_0.001_rate": float(difference.eq(0).mean()) if len(valid) else None,
            "mae": float(difference.abs().mean()) if len(valid) else None,
            "rmse": float(np.sqrt((difference**2).mean())) if len(valid) else None,
            "max_abs_error": float(difference.abs().max()) if len(valid) else None,
        }
    return overlap.reset_index(names="date"), metrics


def write_outputs(
    fs: gcsfs.GCSFileSystem,
    derived: pd.DataFrame,
    combined_panel: pd.DataFrame,
    combined_long: pd.DataFrame,
    overlap: pd.DataFrame,
    report: dict,
    upload: bool,
) -> None:
    LOCAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "ng_front4_v2_derived.parquet": derived,
        "ng_front4_daily_completed.parquet": combined_panel,
        "nymex_futures_front4_daily_completed.csv": combined_long,
        "v2_eia_overlap_validation.csv": overlap,
    }
    for name, frame in outputs.items():
        path = LOCAL_OUTPUT_DIR / name
        if path.suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False)
        print(f"local: {path} ({path.stat().st_size / 1024**2:.2f} MiB)")
        if upload:
            fs.put(str(path), f"{OUTPUT_PREFIX}/{name}")
            print(f"gcs: gs://{OUTPUT_PREFIX}/{name}")

    report_path = LOCAL_OUTPUT_DIR / "v2_backfill_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"local: {report_path}")
    if upload:
        fs.put(str(report_path), f"{OUTPUT_PREFIX}/{report_path.name}")
        print(f"gcs: gs://{OUTPUT_PREFIX}/{report_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--validation-start",
        default="2024-01-02",
        help="First v2 date to extract; overlap is used to validate against EIA.",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()

    fs = gcsfs.GCSFileSystem()
    eia_raw, eia_wide = load_eia(fs)
    files = list_trade_files(fs, pd.Timestamp(args.validation_start))
    if not files:
        raise RuntimeError("No v2 NYMEX trade files found")
    print(
        f"trade files: {len(files)} | "
        f"{files[0].trading_date.date()} -> {files[-1].trading_date.date()}"
    )

    rows: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(extract_trade_file, trade_file, fs): trade_file
            for trade_file in files
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            trade_file = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # retain an auditable failure list
                errors.append(
                    {
                        "date": str(trade_file.trading_date.date()),
                        "key": trade_file.key,
                        "error": repr(exc),
                    }
                )
            if completed % 50 == 0 or completed == len(futures):
                print(
                    f"processed {completed}/{len(futures)} files | "
                    f"records={len(rows):,} errors={len(errors)}"
                )

    if not rows:
        raise RuntimeError("No NG outright settlement-window trades extracted")
    derived = pd.DataFrame(rows).sort_values(["date", "contract_rank"])
    derived_complete, combined_panel, combined_long = build_outputs(
        eia_raw, eia_wide, derived
    )
    overlap, metrics = validation_report(eia_wide, derived_complete)

    cutoff = eia_wide[["c1", "c2", "c3", "c4"]].dropna(how="all").index.max()
    backfill_panel = combined_panel.loc[combined_panel["date"] > cutoff]
    complete_date_set = set(derived_complete["date"])
    incomplete_dates = [
        str(item.trading_date.date())
        for item in files
        if item.trading_date not in complete_date_set
    ]
    report = {
        "source_bucket": f"gs://{SOURCE_BUCKET}",
        "target_bucket": f"gs://{TARGET_BUCKET}",
        "eia_cutoff": str(cutoff.date()),
        "v2_extraction_start": str(derived_complete["date"].min().date()),
        "v2_extraction_end": str(derived_complete["date"].max().date()),
        "backfill_start": (
            str(backfill_panel["date"].min().date()) if len(backfill_panel) else None
        ),
        "backfill_end": (
            str(backfill_panel["date"].max().date()) if len(backfill_panel) else None
        ),
        "backfill_complete_dates": int(len(backfill_panel)),
        "incomplete_or_empty_files": int(
            len(files) - derived_complete["date"].nunique()
        ),
        "incomplete_or_empty_file_dates": incomplete_dates,
        "file_errors": errors,
        "normal_method": NORMAL_METHOD,
        "expiration_method": EXPIRY_METHOD,
        "fallback_method": FALLBACK_METHOD,
        "early_close_method": EARLY_CLOSE_METHOD,
        "source_gap_method": SOURCE_GAP_METHOD,
        "method_counts": {
            key: int(value)
            for key, value in derived_complete["method"].value_counts().items()
        },
        "early_close_dates": [
            str(pd.Timestamp(value).date())
            for value in sorted(
                derived_complete.loc[
                    derived_complete["window_kind"].eq("early_close"), "date"
                ].unique()
            )
        ],
        "source_gap_dates": [
            str(pd.Timestamp(value).date())
            for value in sorted(
                derived_complete.loc[
                    derived_complete["window_kind"].eq("source_gap"), "date"
                ].unique()
            )
        ],
        "price_type": PRICE_TYPE,
        "validation_against_eia_official_settlement": metrics,
        "important_note": (
            "Values after the EIA cutoff are NYMEX tick-trade VWAP proxies, "
            "not official CME/EIA settlement prices."
        ),
    }
    print(json.dumps(report, indent=2))
    write_outputs(
        fs,
        derived_complete,
        combined_panel,
        combined_long,
        overlap,
        report,
        upload=args.upload,
    )


if __name__ == "__main__":
    main()
