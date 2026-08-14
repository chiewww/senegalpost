from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


URL = "https://www.laposte.sn/envoi-colis-lettres-international/"
OUTPUT_FILE = Path("results.txt")

WEIGHT_GRAMS = 10
USER_AGENT = (
    "Mozilla/5.0 (compatible; SenegalPostMonitor/1.0; "
    "+https://github.com/)"
)

REQUEST_TIMEOUT = 40


def log(message: str) -> None:
    print(f"[monitor] {message}", flush=True)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalized_key(value: str) -> str:
    value = normalize_text(value)

    value = unicodedata.normalize("NFD", value)
    value = "".join(
        c for c in value
        if unicodedata.category(c) != "Mn"
    )

    return value.casefold()


def download_page() -> str:
    log(f"Opening {URL}")

    request = Request(
        URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
    )

    with urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        content = response.read()

    # The page is UTF-8.
    html = content.decode("utf-8", errors="replace")

    log(f"Downloaded {len(html):,} bytes")

    return html


def extract_js_object(
    html: str,
    variable_name: str,
) -> dict:
    """
    Extract a JavaScript object assigned like:

        const COURRIER_ZONES = {
            ...
        };

    The current page uses JSON-compatible syntax for these
    particular objects, so json.loads() can parse them directly.

    We deliberately locate the matching closing brace rather than
    using a simple .*? regex, because nested objects are present.
    """

    pattern = re.compile(
        rf"\bconst\s+{re.escape(variable_name)}\s*=\s*\{{",
        re.MULTILINE,
    )

    match = pattern.search(html)

    if not match:
        raise RuntimeError(
            f"Could not find JavaScript object "
            f"{variable_name!r} on the page."
        )

    start = match.end() - 1

    depth = 0
    in_string = False
    string_quote = None
    escaped = False

    end = None

    for position in range(start, len(html)):
        char = html[position]

        if in_string:
            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if char == string_quote:
                in_string = False
                string_quote = None

            continue

        if char in ("'", '"'):
            in_string = True
            string_quote = char
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                end = position + 1
                break

    if end is None:
        raise RuntimeError(
            f"Could not find the end of JavaScript object "
            f"{variable_name!r}."
        )

    object_text = html[start:end]

    try:
        return json.loads(object_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse {variable_name} as JSON-compatible "
            f"JavaScript: {exc}"
        ) from exc


def get_courrier_tranche(weight_grams: int) -> int | None:
    """
    Exact equivalent of the website's getCourrierTranche().
    """

    if weight_grams <= 10:
        return 1

    if weight_grams <= 20:
        return 2

    if weight_grams <= 40:
        return 3

    if weight_grams <= 60:
        return 4

    if weight_grams <= 80:
        return 5

    if weight_grams <= 100:
        return 6

    if weight_grams <= 250:
        return 7

    if weight_grams <= 500:
        return 8

    if weight_grams <= 1000:
        return 9

    if weight_grams <= 2000:
        return 10

    if weight_grams <= 3000:
        return 11

    return None


def get_delay(zone: str) -> str:
    """
    Exact equivalent of the current site's courrier delay logic.
    """

    if zone == "nat":
        return "24-48h"

    return "5-10 jours ouvrés"


def format_fcfa(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " FCFA"


def calculate_country(
    country: str,
    zone: str,
    pricing: dict,
    tranche: int,
) -> dict:
    """
    Calculate one country's 10 g result.

    IMPORTANT:
      None/null and 0 are deliberately treated as different statuses.
    """

    if zone not in pricing:
        return {
            "country": country,
            "zone": zone,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": (
                f"Zone {zone!r} is not present in "
                "PRICING_COURRIER"
            ),
        }

    zone_rates = pricing[zone]

    tranche_key = str(tranche)

    if tranche_key not in zone_rates:
        return {
            "country": country,
            "zone": zone,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": (
                f"Weight tranche {tranche} is not present "
                f"for zone {zone!r}"
            ),
        }

    tariff = zone_rates[tranche_key]

    # ---------------------------------------------------------
    # IMPORTANT:
    # The website uses null to mean that the service is not
    # admitted/available for that zone and weight.
    # ---------------------------------------------------------
    if tariff is None:
        return {
            "country": country,
            "zone": zone,
            "status": "NULL",
            "tariff": None,
            "delay": None,
            "detail": "Service not available (tariff is null)",
        }

    # ---------------------------------------------------------
    # Keep 0 FCFA separate from null.
    # ---------------------------------------------------------
    if tariff == 0:
        return {
            "country": country,
            "zone": zone,
            "status": "ZERO",
            "tariff": 0,
            "delay": get_delay(zone),
            "detail": "Tariff is 0 FCFA",
        }

    if not isinstance(tariff, int):
        return {
            "country": country,
            "zone": zone,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": (
                f"Unexpected tariff value: {tariff!r}"
            ),
        }

    return {
        "country": country,
        "zone": zone,
        "status": "AVAILABLE",
        "tariff": tariff,
        "delay": get_delay(zone),
        "detail": None,
    }


def build_output(
    results: list[dict],
    country_count: int,
    tranche: int,
) -> str:
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    available = [
        result
        for result in results
        if result["status"] == "AVAILABLE"
    ]

    zero = [
        result
        for result in results
        if result["status"] == "ZERO"
    ]

    null_results = [
        result
        for result in results
        if result["status"] == "NULL"
    ]

    errors = [
        result
        for result in results
        if result["status"] == "ERROR"
    ]

    lines = [
        "Senegal La Poste",
        "International Letter Rates",
        "================================",
        "",
        f"Last checked: {timestamp}",
        "Service: Courrier (0 - 3kg)",
        f"Weight: {WEIGHT_GRAMS} grams",
        f"Weight tranche: {tranche}",
        "",
        f"Countries in tariff database: {country_count}",
        "",
        "AVAILABLE SERVICES",
        "==================",
        "",
    ]

    for result in available:
        lines.append(
            f"{result['country']} | "
            f"{format_fcfa(result['tariff'])} | "
            f"{result['delay']}"
        )

    lines.extend(
        [
            "",
            "UNAVAILABLE / NULL TARIFF",
            "=========================",
            "",
        ]
    )

    if null_results:
        for result in null_results:
            lines.append(
                f"{result['country']} | "
                "SERVICE NOT AVAILABLE | "
                "tariff = null"
            )
    else:
        lines.append(
            "None"
        )

    lines.extend(
        [
            "",
            "ZERO TARIFF",
            "===========",
            "",
        ]
    )

    if zero:
        for result in zero:
            lines.append(
                f"{result['country']} | 0 FCFA"
            )
    else:
        lines.append(
            "None"
        )

    if errors:
        lines.extend(
            [
                "",
                "ERRORS / COULD NOT VERIFY",
                "==========================",
                "",
            ]
        )

        for result in errors:
            lines.append(
                f"{result['country']} | "
                f"{result['detail']}"
            )

    lines.extend(
        [
            "",
            "SUMMARY",
            "=======",
            "",
            f"Countries checked: {len(results)}",
            f"Available: {len(available)}",
            f"Null / unavailable: {len(null_results)}",
            f"Zero tariff: {len(zero)}",
            f"Errors: {len(errors)}",
            "",
        ]
    )

    return "\n".join(lines)


def validate_data(
    countries: dict,
    pricing: dict,
) -> None:
    if not countries:
        raise RuntimeError(
            "COURRIER_ZONES is empty."
        )

    if not pricing:
        raise RuntimeError(
            "PRICING_COURRIER is empty."
        )

    log(
        f"Found {len(countries)} countries in "
        "COURRIER_ZONES."
    )

    log(
        f"Found {len(pricing)} pricing zones in "
        "PRICING_COURRIER."
    )

    expected_zones = {
        "nat",
        "z1",
        "z2",
        "z3",
        "z4",
        "z5",
    }

    missing_zones = expected_zones - set(pricing)

    if missing_zones:
        raise RuntimeError(
            "PRICING_COURRIER is missing zones: "
            + ", ".join(sorted(missing_zones))
        )

    missing_country_zones = []

    for country, zone in countries.items():
        if zone not in pricing:
            missing_country_zones.append(
                f"{country} -> {zone}"
            )

    if missing_country_zones:
        preview = ", ".join(
            missing_country_zones[:10]
        )

        raise RuntimeError(
            "Some countries refer to missing pricing zones: "
            + preview
        )


def main() -> int:
    try:
        html = download_page()

        log("Extracting COURRIER_ZONES...")
        countries = extract_js_object(
            html,
            "COURRIER_ZONES",
        )

        log("Extracting PRICING_COURRIER...")
        pricing = extract_js_object(
            html,
            "PRICING_COURRIER",
        )

        validate_data(
            countries,
            pricing,
        )

        tranche = get_courrier_tranche(
            WEIGHT_GRAMS
        )

        if tranche is None:
            raise RuntimeError(
                f"Weight {WEIGHT_GRAMS} g is outside "
                "the 0-3 kg courrier range."
            )

        log(
            f"Weight {WEIGHT_GRAMS} g uses "
            f"tariff tranche {tranche}."
        )

        results = []

        for country, zone in countries.items():
            result = calculate_country(
                country=country,
                zone=zone,
                pricing=pricing,
                tranche=tranche,
            )

            results.append(result)

        # Keep the same order as the website's dictionary,
        # which is useful for detecting meaningful changes.
        #
        # Do NOT silently discard null or zero results.

        null_results = [
            result
            for result in results
            if result["status"] == "NULL"
        ]

        zero_results = [
            result
            for result in results
            if result["status"] == "ZERO"
        ]

        errors = [
            result
            for result in results
            if result["status"] == "ERROR"
        ]

        log(
            f"Countries processed: {len(results)}"
        )

        log(
            f"Available: "
            f"{len(results) - len(null_results) - len(zero_results) - len(errors)}"
        )

        log(
            f"NULL/unavailable: {len(null_results)}"
        )

        log(
            f"Zero tariff: {len(zero_results)}"
        )

        log(
            f"Errors: {len(errors)}"
        )

        # -----------------------------------------------------
        # Safety checks.
        #
        # If the page suddenly changes and we extract only a
        # tiny number of countries, DO NOT replace results.txt.
        # -----------------------------------------------------
        if len(results) < 20:
            raise RuntimeError(
                "Too few countries were extracted "
                f"({len(results)}). "
                "Refusing to overwrite results.txt."
            )

        # If any country has an ERROR, the run is incomplete.
        # A genuine NULL is NOT an error and is intentionally
        # included in the output.
        if errors:
            raise RuntimeError(
                f"{len(errors)} country/countries could not "
                "be calculated. Refusing to overwrite "
                "results.txt."
            )

        output = build_output(
            results=results,
            country_count=len(countries),
            tranche=tranche,
        )

        # Only write results.txt after every validation succeeds.
        OUTPUT_FILE.write_text(
            output,
            encoding="utf-8",
        )

        log(
            f"Successfully wrote {OUTPUT_FILE}."
        )

        if null_results:
            log(
                "IMPORTANT: One or more countries have "
                "a NULL tariff for 10 g."
            )

            for result in null_results:
                log(
                    f"  NULL: {result['country']}"
                )

        if zero_results:
            log(
                "IMPORTANT: One or more countries have "
                "a 0 FCFA tariff for 10 g."
            )

            for result in zero_results:
                log(
                    f"  ZERO: {result['country']}"
                )

        return 0

    except Exception as exc:
        log(f"FATAL ERROR: {exc}")

        # Deliberately do NOT modify results.txt.
        return 1


if __name__ == "__main__":
    sys.exit(main())
