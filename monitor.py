from __future__ import annotations

import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


URL = "https://www.laposte.sn/envoi-colis-lettres-international/"
OUTPUT_FILE = Path("results.txt")

WEIGHT_GRAMS = 10

REQUEST_TIMEOUT = 40

USER_AGENT = (
    "Mozilla/5.0 (compatible; SenegalPostMonitor/1.0)"
)


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
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
    )

    with urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        data = response.read()

    html = data.decode(
        "utf-8",
        errors="replace",
    )

    log(f"Downloaded {len(html):,} bytes")

    return html


def extract_js_object(
    html: str,
    variable_name: str,
) -> str:
    """
    Extract the complete JavaScript object assigned to a
    variable such as:

        const PRICING_COURRIER = {
            ...
        };

    Returns the raw JavaScript object text.

    It deliberately does NOT attempt to parse it as JSON.
    """

    pattern = re.compile(
        rf"\b(?:const|let|var)\s+"
        rf"{re.escape(variable_name)}"
        rf"\s*=\s*\{{",
        re.MULTILINE,
    )

    match = pattern.search(html)

    if not match:
        raise RuntimeError(
            f"Could not find {variable_name} "
            "in the downloaded page."
        )

    start = match.end() - 1

    depth = 0
    in_string = False
    quote = None
    escaped = False

    for position in range(
        start,
        len(html),
    ):
        char = html[position]

        if in_string:
            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if char == quote:
                in_string = False
                quote = None

            continue

        if char in ("'", '"', "`"):
            in_string = True
            quote = char
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                end = position + 1

                object_text = html[
                    start:end
                ]

                log(
                    f"Found {variable_name}: "
                    f"{len(object_text):,} characters"
                )

                return object_text

    raise RuntimeError(
        f"Could not find the closing brace for "
        f"{variable_name}."
    )


def parse_js_string(
    text: str,
    position: int,
) -> tuple[str, int]:
    """
    Parse a single/double quoted JavaScript string.

    Returns:

        (string_value, new_position)
    """

    quote = text[position]

    if quote not in ("'", '"'):
        raise ValueError(
            "Expected JavaScript string."
        )

    position += 1

    result = []

    while position < len(text):
        char = text[position]

        if char == quote:
            return (
                "".join(result),
                position + 1,
            )

        if char == "\\":
            position += 1

            if position >= len(text):
                raise ValueError(
                    "Unterminated escape sequence."
                )

            escaped = text[position]

            escapes = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "b": "\b",
                "f": "\f",
                "\\": "\\",
                "'": "'",
                '"': '"',
            }

            result.append(
                escapes.get(
                    escaped,
                    escaped,
                )
            )

            position += 1
            continue

        result.append(char)
        position += 1

    raise ValueError(
        "Unterminated JavaScript string."
    )


def parse_courrier_zones(
    object_text: str,
) -> dict[str, str]:
    """
    Parse:

        {
            "SENEGAL": "nat",
            "BENIN": "z1",
            ...
        }

    The function intentionally accepts both single and
    double quoted keys.
    """

    pattern = re.compile(
        r"""
        (?P<quote>['"])
        (?P<country>.*?)
        (?P=quote)
        \s*:\s*
        (?P<zonequote>['"])
        (?P<zone>.*?)
        (?P=zonequote)
        """,
        re.VERBOSE,
    )

    result = {}

    for match in pattern.finditer(
        object_text
    ):
        country = match.group("country")
        zone = match.group("zone")

        result[country] = zone

    if not result:
        raise RuntimeError(
            "Could not extract any countries from "
            "COURRIER_ZONES."
        )

    return result


def parse_pricing_courrier(
    object_text: str,
) -> dict[str, dict[str, int | None]]:
    """
    Parse the specific PRICING_COURRIER structure:

        {
            'nat': {
                1: 200,
                ...
            },
            'z1': {
                1: 300,
                ...
            }
        }

    We only need the numeric weight keys and their values.
    """

    result = {}

    # Find each zone.
    zone_pattern = re.compile(
        r"""
        (?P<quote>['"])
        (?P<zone>nat|z1|z2|z3|z4|z5)
        (?P=quote)
        \s*:\s*
        \{
        (?P<body>.*?)
        \}
        """,
        re.VERBOSE | re.DOTALL,
    )

    for zone_match in zone_pattern.finditer(
        object_text
    ):
        zone = zone_match.group("zone")
        body = zone_match.group("body")

        rates = {}

        rate_pattern = re.compile(
            r"""
            (?P<weight>\d+)
            \s*:\s*
            (?P<value>null|-?\d+(?:\.\d+)?)
            """,
            re.VERBOSE,
        )

        for rate_match in rate_pattern.finditer(
            body
        ):
            weight = rate_match.group(
                "weight"
            )

            raw_value = rate_match.group(
                "value"
            )

            if raw_value == "null":
                value = None

            elif "." in raw_value:
                value = float(raw_value)

            else:
                value = int(raw_value)

            rates[weight] = value

        result[zone] = rates

    if not result:
        raise RuntimeError(
            "Could not extract any pricing zones "
            "from PRICING_COURRIER."
        )

    return result


def get_courrier_tranche(
    weight_grams: int,
) -> int | None:

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
    Exact delay logic currently used by the website.
    """

    if zone == "nat":
        return "24-48h"

    return "5-10 jours ouvrés"


def format_fcfa(
    value: int,
) -> str:

    return (
        f"{value:,}"
        .replace(",", " ")
        + " FCFA"
    )


def calculate_country(
    country: str,
    zone: str,
    pricing: dict,
    tranche: int,
) -> dict:

    if zone not in pricing:
        return {
            "country": country,
            "zone": zone,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": (
                f"Pricing zone {zone!r} "
                "was not found."
            ),
        }

    rates = pricing[zone]

    tranche_key = str(tranche)

    if tranche_key not in rates:
        return {
            "country": country,
            "zone": zone,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": (
                f"Weight tranche {tranche} "
                f"is not defined for {zone}."
            ),
        }

    tariff = rates[tranche_key]

    # -----------------------------------------------------
    # NULL = service unavailable.
    # -----------------------------------------------------

    if tariff is None:
        return {
            "country": country,
            "zone": zone,
            "status": "UNAVAILABLE",
            "tariff": None,
            "delay": None,
            "detail": "Tariff is null",
        }

    # -----------------------------------------------------
    # ZERO = service exists but tariff is 0 FCFA.
    # -----------------------------------------------------

    if tariff == 0:
        return {
            "country": country,
            "zone": zone,
            "status": "ZERO",
            "tariff": 0,
            "delay": get_delay(zone),
            "detail": "Tariff is 0 FCFA",
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
    tranche: int,
) -> str:

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    available = [
        x for x in results
        if x["status"] == "AVAILABLE"
    ]

    unavailable = [
        x for x in results
        if x["status"] == "UNAVAILABLE"
    ]

    zero = [
        x for x in results
        if x["status"] == "ZERO"
    ]

    errors = [
        x for x in results
        if x["status"] == "ERROR"
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
        "AVAILABLE SERVICES",
        "==================",
        "",
    ]

    for item in available:
        lines.append(
            f"{item['country']} | "
            f"{format_fcfa(item['tariff'])} | "
            f"{item['delay']}"
        )

    lines.extend([
        "",
        "UNAVAILABLE / NULL TARIFF",
        "=========================",
        "",
    ])

    if unavailable:
        for item in unavailable:
            lines.append(
                f"{item['country']} | "
                "SERVICE NOT AVAILABLE | "
                "tariff = null"
            )
    else:
        lines.append("None")

    lines.extend([
        "",
        "ZERO TARIFF",
        "===========",
        "",
    ])

    if zero:
        for item in zero:
            lines.append(
                f"{item['country']} | 0 FCFA"
            )
    else:
        lines.append("None")

    if errors:
        lines.extend([
            "",
            "ERRORS / COULD NOT VERIFY",
            "==========================",
            "",
        ])

        for item in errors:
            lines.append(
                f"{item['country']} | "
                f"{item['detail']}"
            )

    lines.extend([
        "",
        "SUMMARY",
        "=======",
        "",
        f"Countries checked: {len(results)}",
        f"Available: {len(available)}",
        f"Null / unavailable: {len(unavailable)}",
        f"Zero tariff: {len(zero)}",
        f"Errors: {len(errors)}",
        "",
    ])

    return "\n".join(lines)


def main() -> int:

    try:
        html = download_page()

        log(
            "Extracting COURRIER_ZONES..."
        )

        zones_text = extract_js_object(
            html,
            "COURRIER_ZONES",
        )

        countries = parse_courrier_zones(
            zones_text
        )

        log(
            f"Countries found: {len(countries)}"
        )

        log(
            "Extracting PRICING_COURRIER..."
        )

        pricing_text = extract_js_object(
            html,
            "PRICING_COURRIER",
        )

        pricing = parse_pricing_courrier(
            pricing_text
        )

        log(
            f"Pricing zones found: "
            f"{len(pricing)}"
        )

        expected_zones = {
            "nat",
            "z1",
            "z2",
            "z3",
            "z4",
            "z5",
        }

        missing = (
            expected_zones
            - set(pricing.keys())
        )

        if missing:
            raise RuntimeError(
                "Missing pricing zones: "
                + ", ".join(sorted(missing))
            )

        tranche = get_courrier_tranche(
            WEIGHT_GRAMS
        )

        if tranche is None:
            raise RuntimeError(
                f"{WEIGHT_GRAMS} g is outside "
                "the 0-3 kg range."
            )

        log(
            f"10 g uses tariff tranche "
            f"{tranche}."
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

        errors = [
            x for x in results
            if x["status"] == "ERROR"
        ]

        unavailable = [
            x for x in results
            if x["status"] == "UNAVAILABLE"
        ]

        zero = [
            x for x in results
            if x["status"] == "ZERO"
        ]

        available = [
            x for x in results
            if x["status"] == "AVAILABLE"
        ]

        log(
            f"Available: {len(available)}"
        )

        log(
            f"NULL/unavailable: "
            f"{len(unavailable)}"
        )

        log(
            f"Zero tariff: {len(zero)}"
        )

        log(
            f"Errors: {len(errors)}"
        )

        # Safety check.
        if len(results) < 20:
            raise RuntimeError(
                f"Only {len(results)} countries "
                "were extracted. "
                "Refusing to overwrite results.txt."
            )

        # A parser problem should never result in a
        # misleading results.txt.
        if errors:
            raise RuntimeError(
                f"{len(errors)} countries could not "
                "be processed."
            )

        output = build_output(
            results=results,
            tranche=tranche,
        )

        OUTPUT_FILE.write_text(
            output,
            encoding="utf-8",
        )

        log(
            f"Successfully wrote "
            f"{OUTPUT_FILE}."
        )

        if unavailable:
            log(
                "Countries with NULL tariff:"
            )

            for item in unavailable:
                log(
                    f"  {item['country']}"
                )

        if zero:
            log(
                "Countries with 0 FCFA:"
            )

            for item in zero:
                log(
                    f"  {item['country']}"
                )

        return 0

    except Exception as exc:

        log(
            f"FATAL ERROR: {exc}"
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())
