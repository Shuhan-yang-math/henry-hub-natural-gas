#!/usr/bin/env python3
"""Download wind-relevant GFS fields from NSF NCAR GDEX dataset d084001.

Two official NCAR access paths are supported:

* ``ncss-download`` uses the synchronous THREDDS NetCDF Subset Service. It
  needs no account and is the preferred path for small, reproducible samples.
* ``gdex-control`` and ``gdex-submit`` build or submit an asynchronous GDEX
  dataset-subset request. Submission requires ``GDEX_API_TOKEN``.

The downloader preserves model initialization time, forecast lead, valid time,
source URLs, requested variables, bounding box, byte count, and SHA-256 in a
JSON manifest next to every NetCDF file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests


GDEX_DATASET_ID = "d084001"
GDEX_SUBSET_DATASET_ID = "ds084.1"
GDEX_API_BASE = "https://gdex.ucar.edu/api"
TDS_BASE = "https://tds.gdex.ucar.edu/thredds"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

WIND_VARIABLES = (
    "u-component_of_wind_height_above_ground",
    "v-component_of_wind_height_above_ground",
    "Temperature_height_above_ground",
    "Pressure_height_above_ground",
)


class NcarDataError(RuntimeError):
    """Raised when an NCAR service returns unusable data or metadata."""


@dataclass(frozen=True)
class BoundingBox:
    north: float
    south: float
    west: float
    east: float

    def validate(self) -> "BoundingBox":
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("bbox must satisfy -90 <= south < north <= 90")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("bbox must satisfy -180 <= west < east <= 180")
        return self


# These are deliberately broad meteorological extraction boxes, not ISO or
# balancing-authority boundary definitions.
REGIONS: dict[str, BoundingBox] = {
    "ercot": BoundingBox(north=37.0, south=25.0, west=-107.0, east=-93.0),
    "spp": BoundingBox(north=50.0, south=30.0, west=-107.0, east=-89.0),
    "miso": BoundingBox(north=50.0, south=28.0, west=-105.0, east=-80.0),
    "conus": BoundingBox(north=49.5, south=24.0, west=-125.0, east=-66.5),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_initialization(value: str) -> datetime:
    if not re.fullmatch(r"\d{10}", value):
        raise ValueError("initialization must use YYYYMMDDHH")
    parsed = datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    if parsed.hour not in {0, 6, 12, 18}:
        raise ValueError("GFS initialization hour must be 00, 06, 12, or 18 UTC")
    return parsed


def parse_csv_ints(value: str, *, minimum: int, maximum: int) -> list[int]:
    try:
        values = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise ValueError(f"expected comma-separated integers, got {value!r}") from exc
    if not values:
        raise ValueError("at least one integer is required")
    if values[0] < minimum or values[-1] > maximum:
        raise ValueError(f"values must be between {minimum} and {maximum}")
    return values


def parse_bbox(value: str) -> BoundingBox:
    try:
        north, south, west, east = (float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError("bbox must be north,south,west,east") from exc
    return BoundingBox(north=north, south=south, west=west, east=east).validate()


def source_filename(initialization: datetime, lead_hour: int) -> str:
    return f"gfs.0p25.{initialization:%Y%m%d%H}.f{lead_hour:03d}.grib2"


def source_path(initialization: datetime, lead_hour: int) -> str:
    return (
        f"files/g/{GDEX_DATASET_ID}/{initialization:%Y}/"
        f"{initialization:%Y%m%d}/{source_filename(initialization, lead_hour)}"
    )


def catalog_url(initialization: datetime) -> str:
    return (
        f"{TDS_BASE}/catalog/files/g/{GDEX_DATASET_ID}/"
        f"{initialization:%Y}/{initialization:%Y%m%d}/catalog.xml"
    )


def ncss_url(initialization: datetime, lead_hour: int) -> str:
    return f"{TDS_BASE}/ncss/grid/{source_path(initialization, lead_hour)}"


def _axis_values(axis: ElementTree.Element) -> list[float]:
    values = axis.find("values")
    if values is None:
        return []
    if values.text and values.text.strip():
        return [float(value) for value in values.text.split()]
    count = int(values.attrib.get("npts", "0"))
    if count == 1:
        return [float(values.attrib["start"])]
    if count > 1 and "resolution" in values.attrib:
        start = float(values.attrib["start"])
        resolution = float(values.attrib["resolution"])
        return [start + resolution * index for index in range(count)]
    return []


def parse_height_capabilities(
    capability_xml: bytes,
    requested_variables: Iterable[str],
) -> dict[str, set[float]]:
    """Return available height-above-ground coordinates for each variable."""
    root = ElementTree.fromstring(capability_xml)
    axes = {
        axis.attrib["name"]: set(_axis_values(axis))
        for axis in root.findall("axis")
        if axis.attrib.get("name", "").startswith("height_above_ground")
    }
    capabilities: dict[str, set[float]] = {}
    requested = set(requested_variables)
    for grid_set in root.iter("gridSet"):
        axis_name = next(
            (
                name
                for name in grid_set.attrib.get("name", "").split()
                if name.startswith("height_above_ground")
            ),
            None,
        )
        if axis_name is None:
            continue
        for grid in grid_set.findall("grid"):
            name = grid.attrib.get("name")
            if name in requested:
                capabilities[name] = axes.get(axis_name, set())
    return capabilities


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 180.0,
        retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "braeswood-naturalgas-ncar-wind-research/1.0"}
        )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout_seconds, **kwargs
                )
                if response.status_code >= 500 and attempt < self.retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
        raise NcarDataError(f"request failed after {self.retries} attempts: {url}") from last_error


class NcssDownloader:
    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def available_source_paths(self, initialization: datetime) -> set[str]:
        url = catalog_url(initialization)
        response = self.client.request("GET", url)
        if response.status_code != 200:
            raise NcarDataError(
                f"NCAR catalog returned HTTP {response.status_code} for {url}"
            )
        root = ElementTree.fromstring(response.content)
        namespace = {
            "t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"
        }
        return {
            item.attrib["urlPath"]
            for item in root.findall(".//t:dataset", namespace)
            if item.attrib.get("urlPath")
        }

    def height_capabilities(
        self,
        initialization: datetime,
        lead_hour: int,
    ) -> tuple[dict[str, set[float]], str]:
        url = f"{ncss_url(initialization, lead_hour)}/dataset.xml"
        response = self.client.request("GET", url)
        if response.status_code != 200:
            raise NcarDataError(
                f"NCSS capability request returned HTTP {response.status_code} for {url}"
            )
        return parse_height_capabilities(response.content, WIND_VARIABLES), url

    def download_one(
        self,
        *,
        initialization: datetime,
        lead_hour: int,
        heights_m: list[int],
        bbox: BoundingBox,
        region_name: str,
        output_dir: Path,
        max_bytes: int,
        overwrite: bool,
    ) -> Path:
        capabilities, capability_url = self.height_capabilities(
            initialization, lead_hour
        )
        variables = [
            variable
            for variable in WIND_VARIABLES
            if set(map(float, heights_m)).intersection(
                capabilities.get(variable, set())
            )
        ]
        required_wind = set(WIND_VARIABLES[:2])
        if not required_wind.issubset(variables):
            missing = sorted(required_wind.difference(variables))
            raise NcarDataError(
                f"required wind variables unavailable at requested heights: {missing}"
            )
        for height_m in heights_m:
            missing_at_height = [
                variable
                for variable in WIND_VARIABLES[:2]
                if float(height_m) not in capabilities.get(variable, set())
            ]
            if missing_at_height:
                raise NcarDataError(
                    f"required wind variables unavailable at {height_m} m: "
                    f"{missing_at_height}"
                )

        init_key = initialization.strftime("%Y%m%d%H")
        valid_time = initialization + timedelta(hours=lead_hour)
        height_key = "-".join(f"{height:03d}" for height in heights_m)
        filename = (
            f"gfs.0p25.{init_key}.f{lead_hour:03d}."
            f"{region_name}.h{height_key}.nc"
        )
        destination = (
            output_dir
            / GDEX_DATASET_ID
            / initialization.strftime("%Y")
            / initialization.strftime("%Y%m%d")
            / init_key
            / filename
        )
        manifest_path = destination.with_suffix(destination.suffix + ".json")
        if destination.exists() and manifest_path.exists() and not overwrite:
            with destination.open("rb") as stream:
                if stream.read(3) == b"CDF":
                    print(f"skip existing {destination}")
                    return destination

        params: list[tuple[str, str]] = [
            *(("var", variable) for variable in variables),
            ("north", str(bbox.north)),
            ("south", str(bbox.south)),
            ("west", str(bbox.west)),
            ("east", str(bbox.east)),
            ("horizStride", "1"),
            ("addLatLon", "true"),
            ("accept", "netcdf"),
        ]
        url = ncss_url(initialization, lead_hour)
        response = self.client.request("GET", url, params=params, stream=True)
        if response.status_code != 200:
            detail = response.text[:500].strip()
            raise NcarDataError(
                f"NCSS returned HTTP {response.status_code}: {detail or 'no response body'}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise NcarDataError(
                            f"NCSS response exceeded --max-bytes={max_bytes:,}"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            with temporary.open("rb") as stream:
                if stream.read(3) != b"CDF":
                    raise NcarDataError("NCSS response is not a classic NetCDF file")
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        query_url = f"{url}?{urlencode(params)}"
        manifest = {
            "dataset_id": GDEX_DATASET_ID,
            "dataset_title": "NCEP GFS 0.25 Degree Global Forecast Grids Historical Archive",
            "data_format": "NetCDF classic generated by NCAR THREDDS NCSS",
            "initialization_time_utc": initialization.isoformat(),
            "forecast_lead_hours": lead_hour,
            "valid_time_utc": valid_time.isoformat(),
            "requested_heights_m": heights_m,
            "variables": variables,
            "available_heights_m_by_variable": {
                variable: sorted(capabilities.get(variable, set()))
                for variable in variables
            },
            "ncss_vertical_selection": (
                "All source height coordinates are retained because NCSS "
                "cannot combine variables whose complete vertical axes differ."
            ),
            "bbox": asdict(bbox),
            "region_label": region_name,
            "source_grib2_file": source_filename(initialization, lead_hour),
            "source_catalog_url": catalog_url(initialization),
            "capability_url": capability_url,
            "ncss_request_url": query_url,
            "retrieved_at_utc": utc_now_iso(),
            "bytes": byte_count,
            "sha256": digest.hexdigest(),
            "license": "CC BY 4.0",
            "license_url": LICENSE_URL,
        }
        manifest_temporary = manifest_path.with_suffix(manifest_path.suffix + ".part")
        manifest_temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temporary, manifest_path)
        print(f"downloaded {destination} ({byte_count:,} bytes)")
        return destination

    def download(
        self,
        *,
        initialization: datetime,
        lead_hours: list[int],
        heights_m: list[int],
        bbox: BoundingBox,
        region_name: str,
        output_dir: Path,
        max_bytes: int,
        overwrite: bool,
    ) -> list[Path]:
        available = self.available_source_paths(initialization)
        missing = [
            lead
            for lead in lead_hours
            if source_path(initialization, lead) not in available
        ]
        if missing:
            raise NcarDataError(
                "source files are absent from the NCAR catalog for lead hours "
                f"{missing}; choose forecast leads listed for {initialization:%Y-%m-%d}"
            )

        outputs = []
        for lead_hour in lead_hours:
            outputs.append(
                self.download_one(
                    initialization=initialization,
                    lead_hour=lead_hour,
                    heights_m=heights_m,
                    bbox=bbox,
                    region_name=region_name,
                    output_dir=output_dir,
                    max_bytes=max_bytes,
                    overwrite=overwrite,
                )
            )
        return outputs


def gdex_product(lead_hour: int) -> str:
    return "Analysis" if lead_hour == 0 else f"{lead_hour}-hour Forecast"


def build_gdex_control(
    *,
    initialization: datetime,
    lead_hours: list[int],
    heights_m: list[int],
    bbox: BoundingBox,
    output_format: str | None,
) -> dict[str, str]:
    control = {
        "dataset": GDEX_SUBSET_DATASET_ID,
        "date": (
            f"{initialization:%Y%m%d%H%M}/to/"
            f"{initialization:%Y%m%d%H%M}"
        ),
        "datetype": "init",
        "param": "U GRD/V GRD/PRES/TMP",
        "level": "HTGL:" + "/".join(str(height) for height in heights_m),
        "nlat": str(bbox.north),
        "slat": str(bbox.south),
        "wlon": str(bbox.west),
        "elon": str(bbox.east),
        "product": "/".join(gdex_product(lead) for lead in lead_hours),
    }
    if output_format:
        control["oformat"] = output_format
    return control


class GdexApi:
    def __init__(self, client: HttpClient, token: str) -> None:
        if not token.strip():
            raise ValueError("GDEX API token is empty")
        self.client = client
        self.token = token.strip()

    def _json_request(
        self, method: str, endpoint: str, *, body: dict[str, str] | None = None
    ) -> dict[str, Any]:
        url = f"{GDEX_API_BASE}/{endpoint.lstrip('/')}"
        response = self.client.request(
            method,
            url,
            params={"token": self.token},
            json=body,
        )
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise NcarDataError(
                f"GDEX API returned non-JSON HTTP {response.status_code} for {endpoint}"
            ) from exc
        errors = payload.get("error_messages") or payload.get("messages") or []
        if response.status_code != 200 or payload.get("status") != "ok":
            raise NcarDataError(
                f"GDEX API {endpoint} failed with HTTP {response.status_code}: {errors}"
            )
        return payload

    def submit(self, control: dict[str, str]) -> dict[str, Any]:
        return self._json_request("POST", "submit/", body=control)

    def status(self, request_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d+", request_id):
            raise ValueError("request id must contain digits only")
        return self._json_request("GET", f"status/{request_id}")

    def files(self, request_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d+", request_id):
            raise ValueError("request id must contain digits only")
        return self._json_request("GET", f"get_req_files/{request_id}")


def response_data(payload: dict[str, Any]) -> Any:
    return payload.get("data", payload.get("result"))


def add_common_subset_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--init", required=True, help="GFS run in YYYYMMDDHH UTC")
    parser.add_argument(
        "--lead-hours",
        default="0,3,6,12,24,48,72",
        help="comma-separated forecast leads",
    )
    parser.add_argument(
        "--heights",
        default="80,100",
        help="comma-separated hub heights in metres",
    )
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--region", choices=sorted(REGIONS), default="ercot")
    location.add_argument("--bbox", help="north,south,west,east")


def subset_arguments(args: argparse.Namespace) -> tuple[
    datetime, list[int], list[int], BoundingBox, str
]:
    initialization = parse_initialization(args.init)
    leads = parse_csv_ints(args.lead_hours, minimum=0, maximum=384)
    heights = parse_csv_ints(args.heights, minimum=1, maximum=300)
    if args.bbox:
        bbox = parse_bbox(args.bbox)
        region = "custom"
    else:
        bbox = REGIONS[args.region].validate()
        region = args.region
    return initialization, leads, heights, bbox, region


def token_from_environment() -> str:
    token = os.environ.get("GDEX_API_TOKEN", "").strip()
    if not token:
        raise NcarDataError(
            "GDEX_API_TOKEN is not set; copy the token from "
            "https://gdex.ucar.edu/accounts/profile/"
        )
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=3)
    commands = parser.add_subparsers(dest="command", required=True)

    ncss = commands.add_parser(
        "ncss-download",
        help="download small regional NetCDF subsets without authentication",
    )
    add_common_subset_arguments(ncss)
    ncss.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "ncar_gdex",
    )
    ncss.add_argument("--max-bytes", type=int, default=250_000_000)
    ncss.add_argument("--overwrite", action="store_true")

    control = commands.add_parser(
        "gdex-control",
        help="print a GDEX asynchronous subset control object; no token needed",
    )
    add_common_subset_arguments(control)
    control.add_argument(
        "--output-format",
        choices=("netCDF", "csv"),
        help="omit to retain native GRIB2",
    )

    submit = commands.add_parser(
        "gdex-submit",
        help="submit an asynchronous GDEX subset request using GDEX_API_TOKEN",
    )
    add_common_subset_arguments(submit)
    submit.add_argument(
        "--output-format",
        choices=("netCDF", "csv"),
        help="omit to retain native GRIB2",
    )

    status = commands.add_parser("gdex-status", help="check an asynchronous request")
    status.add_argument("request_id")

    files = commands.add_parser(
        "gdex-files", help="list output files for a completed asynchronous request"
    )
    files.add_argument("request_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.retries < 1:
        parser.error("--retries must be at least 1")
    client = HttpClient(timeout_seconds=args.timeout, retries=args.retries)

    try:
        if args.command == "ncss-download":
            initialization, leads, heights, bbox, region = subset_arguments(args)
            NcssDownloader(client).download(
                initialization=initialization,
                lead_hours=leads,
                heights_m=heights,
                bbox=bbox,
                region_name=region,
                output_dir=args.output_dir,
                max_bytes=args.max_bytes,
                overwrite=args.overwrite,
            )
            return 0

        if args.command in {"gdex-control", "gdex-submit"}:
            initialization, leads, heights, bbox, _ = subset_arguments(args)
            control = build_gdex_control(
                initialization=initialization,
                lead_hours=leads,
                heights_m=heights,
                bbox=bbox,
                output_format=args.output_format,
            )
            if args.command == "gdex-control":
                print(json.dumps(control, indent=2, sort_keys=True))
            else:
                payload = GdexApi(client, token_from_environment()).submit(control)
                print(json.dumps(response_data(payload), indent=2, sort_keys=True))
            return 0

        api = GdexApi(client, token_from_environment())
        if args.command == "gdex-status":
            payload = api.status(args.request_id)
        elif args.command == "gdex-files":
            payload = api.files(args.request_id)
        else:
            parser.error(f"unsupported command {args.command}")
        print(json.dumps(response_data(payload), indent=2, sort_keys=True))
        return 0
    except (NcarDataError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
