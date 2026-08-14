from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


URL = "https://www.laposte.sn/envoi-colis-lettres-international/"
OUTPUT_FILE = Path("results.txt")
DIAGNOSTICS_DIR = Path("diagnostics")

WEIGHT_GRAMS = "10"

PAGE_TIMEOUT = 40
SHORT_TIMEOUT = 5
CALCULATION_TIMEOUT = 20

# The page currently uses these labels.
COURIER_TAB_TEXT = "Courrier (0 - 3kg)"
CALCULATE_TEXT = "Calculer le tarif"

# The simulator can expose the same destination field more than once
# because the page also contains the parcel simulator. We therefore
# identify the letter simulator by the nearby weight field and/or tab.
DESTINATION_PLACEHOLDER_PARTS = (
    "Saisissez un pays",
)

WEIGHT_PLACEHOLDER_PARTS = (
    "Ex: 20",
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
        c for c in value if unicodedata.category(c) != "Mn"
    )
    return value.casefold()


def create_driver() -> webdriver.Chrome:
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=fr-FR")

    # Avoid unnecessary automation prompts/noise.
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")

    # GitHub-hosted runners sometimes have limited shared memory.
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_TIMEOUT)

    return driver


def save_diagnostics(driver: webdriver.Chrome, name: str) -> None:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)

    try:
        driver.save_screenshot(
            str(DIAGNOSTICS_DIR / f"{safe_name}.png")
        )
    except Exception as exc:
        log(f"Could not save screenshot: {exc}")

    try:
        html = driver.page_source
        (DIAGNOSTICS_DIR / f"{safe_name}.html").write_text(
            html,
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"Could not save HTML: {exc}")


def visible_elements(driver, by, value):
    elements = driver.find_elements(by, value)
    return [element for element in elements if element.is_displayed()]


def find_courrier_tab(driver: webdriver.Chrome):
    candidates = []

    # Exact visible text.
    candidates.extend(
        visible_elements(
            driver,
            By.XPATH,
            "//*[self::button or self::a or @role='button']"
            "[contains(normalize-space(.), 'Courrier (0 - 3kg)')]",
        )
    )

    if not candidates:
        # More tolerant text search.
        candidates.extend(
            visible_elements(
                driver,
                By.XPATH,
                "//*[contains(normalize-space(.), 'Courrier')]"
                "[contains(normalize-space(.), '3kg')]",
            )
        )

    if not candidates:
        raise RuntimeError(
            "Could not find the 'Courrier (0 - 3kg)' tab."
        )

    return candidates[0]


def select_courrier(driver: webdriver.Chrome) -> None:
    log("Selecting Courrier (0 - 3kg)...")

    tab = WebDriverWait(driver, PAGE_TIMEOUT).until(
        lambda d: find_courrier_tab(d)
    )

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        tab,
    )

    try:
        tab.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", tab)

    time.sleep(1)


def find_weight_input(driver: webdriver.Chrome):
    # First try placeholder.
    for placeholder in WEIGHT_PLACEHOLDER_PARTS:
        elements = visible_elements(
            driver,
            By.XPATH,
            f"//input[contains(@placeholder, '{placeholder}')]",
        )

        if elements:
            return elements[0]

    # Fall back to an input associated with the visible label.
    elements = visible_elements(
        driver,
        By.XPATH,
        "//input[@type='number' or @type='text']",
    )

    # Prefer an input whose nearby parent/label contains "grammes".
    for element in elements:
        try:
            parent_text = normalize_text(
                element.find_element(
                    By.XPATH,
                    "./ancestor::*[self::div or self::label][1]",
                ).text
            ).casefold()

            if "gramme" in parent_text:
                return element
        except Exception:
            pass

    raise RuntimeError("Could not find the letter weight input.")


def find_destination_inputs(driver: webdriver.Chrome):
    elements = []

    for placeholder in DESTINATION_PLACEHOLDER_PARTS:
        elements.extend(
            visible_elements(
                driver,
                By.XPATH,
                f"//input[contains(@placeholder, '{placeholder}')]",
            )
        )

    # Remove duplicate WebElements by DOM id/reference where possible.
    unique = []
    seen = set()

    for element in elements:
        try:
            identifier = element.id
        except Exception:
            identifier = str(id(element))

        if identifier not in seen:
            seen.add(identifier)
            unique.append(element)

    return unique


def find_letter_destination_input(driver: webdriver.Chrome):
    inputs = find_destination_inputs(driver)

    if not inputs:
        raise RuntimeError(
            "Could not find a destination country input."
        )

    # If there are two destination inputs, identify the letter one
    # using the nearest surrounding text.
    for element in inputs:
        try:
            ancestor = element.find_element(
                By.XPATH,
                "./ancestor::div[1]",
            )

            text = normalize_text(ancestor.text).casefold()

            if "poids" not in text or "kg" not in text:
                return element
        except Exception:
            pass

    # In the current page structure, the first destination field belongs
    # to the Courrier simulator.
    return inputs[0]


def set_weight(driver: webdriver.Chrome) -> None:
    log(f"Setting weight to {WEIGHT_GRAMS} grams...")

    weight = WebDriverWait(driver, PAGE_TIMEOUT).until(
        lambda d: find_weight_input(d)
    )

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        weight,
    )

    weight.click()
    weight.send_keys(Keys.CONTROL, "a")
    weight.send_keys(WEIGHT_GRAMS)

    # Trigger frameworks listening to input/change events.
    driver.execute_script(
        """
        arguments[0].dispatchEvent(
            new Event('input', {bubbles: true})
        );
        arguments[0].dispatchEvent(
            new Event('change', {bubbles: true})
        );
        """,
        weight,
    )


def get_autocomplete_options(driver: webdriver.Chrome) -> list[str]:
    selectors = [
        "[role='option']",
        "[role='listbox'] li",
        "ul[role='listbox'] li",
        "ul li",
    ]

    found = []

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)

            for element in elements:
                if not element.is_displayed():
                    continue

                text = normalize_text(element.text)

                if not text:
                    continue

                # Ignore generic UI entries.
                if text.casefold() in {
                    "courrier (0 - 3kg)",
                    "colis (1 - 30kg)",
                }:
                    continue

                found.append(text)
        except Exception:
            continue

    result = []
    seen = set()

    for item in found:
        key = normalized_key(item)

        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


def type_search(driver: webdriver.Chrome, text: str) -> None:
    destination = find_letter_destination_input(driver)

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        destination,
    )

    destination.click()
    destination.send_keys(Keys.CONTROL, "a")
    destination.send_keys(text)

    # Give the autocomplete JavaScript time to react.
    time.sleep(0.35)


def discover_countries(driver: webdriver.Chrome) -> list[str]:
    """
    Discover country names from the site's autocomplete.

    We deliberately don't maintain a hard-coded list of countries.
    The site's autocomplete is the source of truth.

    A first pass uses single letters. If the site limits the number
    of suggestions, a second pass uses two-letter combinations.
    """

    log("Discovering countries from the destination autocomplete...")

    countries: dict[str, str] = {}

    # First pass: a-z.
    for code in range(ord("a"), ord("z") + 1):
        letter = chr(code)

        try:
            type_search(driver, letter)
            options = get_autocomplete_options(driver)

            for country in options:
                key = normalized_key(country)

                # Filter out obviously unrelated entries.
                if len(country) >= 2 and key not in countries:
                    countries[key] = country

        except Exception as exc:
            log(f"Autocomplete search '{letter}' failed: {exc}")

    log(f"Countries found after first pass: {len(countries)}")

    # If very few entries are found, try two-letter prefixes.
    # This also helps when the widget only displays a limited number
    # of results for a single-character query.
    if len(countries) < 100:
        log("Running second autocomplete discovery pass...")

        for first in "abcdefghijklmnopqrstuvwxyz":
            for second in "abcdefghijklmnopqrstuvwxyz":
                prefix = first + second

                try:
                    type_search(driver, prefix)
                    options = get_autocomplete_options(driver)

                    for country in options:
                        key = normalized_key(country)

                        if len(country) >= 2 and key not in countries:
                            countries[key] = country

                except Exception:
                    # One bad prefix should never abort discovery.
                    continue

        log(
            f"Countries found after second pass: "
            f"{len(countries)}"
        )

    # Remove obvious non-country UI text.
    excluded = {
        normalized_key("Saisissez un pays..."),
        normalized_key("Pays de destination"),
    }

    result = [
        name
        for key, name in countries.items()
        if key not in excluded
    ]

    result.sort(key=normalized_key)

    if len(result) < 20:
        raise RuntimeError(
            "Country discovery returned too few countries "
            f"({len(result)}). Refusing to create an incomplete "
            "results.txt."
        )

    log(f"Final discovered country count: {len(result)}")

    return result


def clear_destination(driver: webdriver.Chrome) -> None:
    destination = find_letter_destination_input(driver)

    destination.click()
    destination.send_keys(Keys.CONTROL, "a")
    destination.send_keys(Keys.BACKSPACE)

    time.sleep(0.2)


def select_country(
    driver: webdriver.Chrome,
    country: str,
) -> None:
    destination = find_letter_destination_input(driver)

    destination.click()
    destination.send_keys(Keys.CONTROL, "a")
    destination.send_keys(country)

    # Wait for autocomplete.
    time.sleep(0.5)

    options = get_autocomplete_options(driver)

    target = None
    country_key = normalized_key(country)

    # Exact normalized match first.
    for option_text in options:
        if normalized_key(option_text) == country_key:
            target = option_text
            break

    # Otherwise allow the first option beginning with the country.
    if target is None:
        for option_text in options:
            if normalized_key(option_text).startswith(country_key):
                target = option_text
                break

    if target is not None:
        # Find the actual visible option again.
        selectors = [
            "[role='option']",
            "[role='listbox'] li",
            "ul[role='listbox'] li",
            "ul li",
        ]

        for selector in selectors:
            elements = driver.find_elements(
                By.CSS_SELECTOR,
                selector,
            )

            for element in elements:
                if not element.is_displayed():
                    continue

                text = normalize_text(element.text)

                if normalized_key(text) == normalized_key(target):
                    try:
                        element.click()
                    except ElementClickInterceptedException:
                        driver.execute_script(
                            "arguments[0].click();",
                            element,
                        )

                    time.sleep(0.3)
                    return

    # Keyboard fallback. Autocomplete widgets commonly accept Enter
    # after the exact country has been typed.
    destination.send_keys(Keys.ARROW_DOWN)
    destination.send_keys(Keys.ENTER)
    time.sleep(0.3)


def find_calculate_button(driver: webdriver.Chrome):
    buttons = visible_elements(
        driver,
        By.XPATH,
        "//button[contains(normalize-space(.), 'Calculer le tarif')]"
        " | //*[@role='button' and contains(normalize-space(.), 'Calculer le tarif')]",
    )

    if buttons:
        return buttons[0]

    # Tolerate a typo/variation in the site's capitalization.
    buttons = visible_elements(
        driver,
        By.XPATH,
        "//*[self::button or @role='button']"
        "[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), "
        "'calculer le tarif')]",
    )

    if buttons:
        return buttons[0]

    raise RuntimeError("Could not find the calculation button.")


def click_calculate(driver: webdriver.Chrome) -> None:
    button = WebDriverWait(driver, PAGE_TIMEOUT).until(
        lambda d: find_calculate_button(d)
    )

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        button,
    )

    try:
        button.click()
    except ElementClickInterceptedException:
        driver.execute_script(
            "arguments[0].click();",
            button,
        )


def extract_result_text(driver: webdriver.Chrome) -> str:
    """
    Read the visible text from the result area.

    We search for the distinctive result labels instead of relying
    on a fragile CSS class.
    """

    def result_present(d):
        text = normalize_text(d.find_element(By.TAG_NAME, "body").text)

        return (
            "Estimation du tarif de l'envoi" in text
            or "Estimation du tarif de l’envoi" in text
            or "Délai estimé" in text
            or "Delai estime" in text
        )

    WebDriverWait(driver, CALCULATION_TIMEOUT).until(result_present)

    body_text = driver.find_element(By.TAG_NAME, "body").text

    # Keep only useful lines for easier parsing.
    lines = [
        normalize_text(line)
        for line in body_text.splitlines()
        if normalize_text(line)
    ]

    return "\n".join(lines)


def parse_tariff(text: str) -> int | None:
    """
    Return tariff in FCFA.

    Examples:
      2500 FCFA -> 2500
      2 500 FCFA -> 2500
      0 FCFA -> 0
    """

    patterns = [
        r"Estimation du tarif de l['’]envoi\s*([0-9][0-9\s.,]*)\s*FCFA",
        r"([0-9][0-9\s.,]*)\s*FCFA",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            raw = match.group(1)
            raw = raw.replace(" ", "")
            raw = raw.replace("\u00a0", "")
            raw = raw.replace(".", "")
            raw = raw.replace(",", "")

            try:
                return int(raw)
            except ValueError:
                pass

    return None


def parse_delay(text: str) -> str | None:
    patterns = [
        r"Délai estimé\s*:?\s*([^\n]+)",
        r"Delai estime\s*:?\s*([^\n]+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return normalize_text(match.group(1))

    # The site may display something such as "3-10 jours ouvrés".
    match = re.search(
        r"([0-9]+\s*[-–]\s*[0-9]+\s*jours(?:\s+\w+)?)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_text(match.group(1))

    return None


def calculate_country(
    driver: webdriver.Chrome,
    country: str,
) -> dict:
    log(f"Calculating: {country}")

    try:
        select_country(driver, country)
        click_calculate(driver)

        result_text = extract_result_text(driver)

        tariff = parse_tariff(result_text)
        delay = parse_delay(result_text)

        if tariff is None:
            return {
                "country": country,
                "status": "ERROR",
                "tariff": None,
                "delay": None,
                "detail": "Could not read tariff",
            }

        if tariff == 0:
            return {
                "country": country,
                "status": "ZERO",
                "tariff": 0,
                "delay": delay,
                "detail": None,
            }

        if not delay:
            return {
                "country": country,
                "status": "ERROR",
                "tariff": tariff,
                "delay": None,
                "detail": "Could not read estimated delay",
            }

        return {
            "country": country,
            "status": "AVAILABLE",
            "tariff": tariff,
            "delay": delay,
            "detail": None,
        }

    except TimeoutException:
        return {
            "country": country,
            "status": "UNAVAILABLE",
            "tariff": None,
            "delay": None,
            "detail": "No calculation result appeared",
        }

    except Exception as exc:
        log(f"Error calculating {country}: {exc}")

        return {
            "country": country,
            "status": "ERROR",
            "tariff": None,
            "delay": None,
            "detail": str(exc),
        }


def build_output(results: list[dict]) -> str:
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

    unavailable = [
        result
        for result in results
        if result["status"] == "UNAVAILABLE"
    ]

    errors = [
        result
        for result in results
        if result["status"] == "ERROR"
    ]

    lines = [
        f"Last checked: {timestamp}",
        "",
        "Senegal La Poste — International Letter Rates",
        "Service: Courrier (0 - 3kg)",
        "Weight: 10 grams",
        "",
        "AVAILABLE SERVICES",
        "==================",
        "",
    ]

    for result in available:
        tariff = f"{result['tariff']} FCFA"
        delay = result["delay"]

        lines.append(
            f"{result['country']} | {tariff} | {delay}"
        )

    lines.extend(
        [
            "",
            "UNAVAILABLE / ZERO-TARIFF SERVICES",
            "===================================",
            "",
        ]
    )

    for result in zero:
        lines.append(
            f"{result['country']} | 0 FCFA"
        )

    for result in unavailable:
        lines.append(
            f"{result['country']} | SERVICE NOT AVAILABLE"
        )

    # Errors are kept separate so a website failure isn't mistaken
    # for a genuine "unavailable" country.
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
            detail = result.get("detail") or "Unknown error"

            lines.append(
                f"{result['country']} | ERROR | {detail}"
            )

    lines.extend(
        [
            "",
            f"Countries checked: {len(results)}",
            f"Available: {len(available)}",
            f"Zero tariff: {len(zero)}",
            f"Unavailable: {len(unavailable)}",
            f"Errors: {len(errors)}",
            "",
        ]
    )

    return "\n".join(lines)


def main() -> int:
    driver = None

    try:
        DIAGNOSTICS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        driver = create_driver()

        log(f"Opening {URL}")
        driver.get(URL)

        WebDriverWait(driver, PAGE_TIMEOUT).until(
            lambda d: d.execute_script(
                "return document.readyState"
            ) == "complete"
        )

        time.sleep(2)

        select_courrier(driver)
        set_weight(driver)

        countries = discover_countries(driver)

        # Start again from a clean state before processing.
        select_courrier(driver)
        set_weight(driver)

        results = []

        for index, country in enumerate(countries, start=1):
            log(
                f"[{index}/{len(countries)}] "
                f"{country}"
            )

            result = calculate_country(
                driver,
                country,
            )

            results.append(result)

            # Small delay to avoid hammering the site.
            time.sleep(0.5)

        # Never overwrite results.txt with an obviously failed run.
        errors = [
            result
            for result in results
            if result["status"] == "ERROR"
        ]

        if len(results) < 20:
            raise RuntimeError(
                "Too few countries were successfully processed."
            )

        if len(errors) > max(5, int(len(results) * 0.25)):
            raise RuntimeError(
                f"Too many calculation errors: "
                f"{len(errors)}/{len(results)}"
            )

        output = build_output(results)
        OUTPUT_FILE.write_text(
            output,
            encoding="utf-8",
        )

        log(
            f"Wrote {OUTPUT_FILE} "
            f"with {len(results)} countries."
        )

        return 0

    except Exception as exc:
        log(f"FATAL ERROR: {exc}")

        if driver is not None:
            save_diagnostics(
                driver,
                "fatal-error",
            )

        return 1

    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    sys.exit(main())
