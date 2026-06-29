#!/usr/bin/env python3
"""
Daily Hertz + AAA rental monitor for GitHub Actions.

This version is intentionally defensive: it retries multiple Hertz entry URLs,
handles banners/modals more carefully, and captures richer debug artifacts when
the booking form is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, List, Optional

import requests
from playwright.sync_api import sync_playwright


HERTZ_HOME_URL = "https://www.hertz.com/us/en"
HERTZ_BOOKING_URLS = [
    "https://www.hertz.com/us/en",
    "https://www.hertz.com/us/en/reservation",
    "https://www.hertz.com/rentacar/reservation/",
]
AAA_OFFER_URL = "https://www.hertz.com/us/en/deals-and-offers/aaa/aaa-paynow"
ARTIFACT_DIR = Path("artifacts")
DEFAULT_TARGET_TOTAL = 700.0
DEFAULT_LOCATION_CANDIDATES = [
    "Fairfax, VA",
    "Dulles - Dulles International Airport (IAD)",
    "Washington, DC - Ronald Reagan Washington National Airport (DCA)",
]
DEFAULT_DATE_OFFSETS = [0, 1, 2, 3, 7, 14]
DEFAULT_RENTAL_LENGTHS = [28, 30, 31, 35]


@dataclass
class QuoteCandidate:
    pickup_location: str
    pickup_date: str
    return_date: str
    aaa_applied: bool
    total_price: Optional[float]
    total_text: str
    vehicle_summary: str
    booking_url: str
    notes: str = ""


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_json(name: str, default):
    value = os.getenv(name)
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def ensure_artifacts() -> None:
    ARTIFACT_DIR.mkdir(exist_ok=True)


def fetch_aaa_offer_details() -> dict:
    details = {
        "headline": "AAA members: Pay Now to Pay Less",
        "book_by": "Unknown",
        "offer_url": AAA_OFFER_URL,
    }

    try:
        response = requests.get(
            AAA_OFFER_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 HertzAAAMonitor/1.0"},
        )
        response.raise_for_status()
        text = re.sub(r"\s+", " ", response.text)
        match = re.search(r"Book by\s*([0-9/]+)", text, re.I)
        if match:
            details["book_by"] = match.group(1)
    except Exception as exc:
        details["error"] = str(exc)

    return details


def parse_price(text: str) -> Optional[float]:
    match = re.search(r"\$([0-9,]+(?:\.[0-9]{2})?)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def pick_best_quote(quotes: Iterable[QuoteCandidate]) -> Optional[QuoteCandidate]:
    quotes = list(quotes)
    valid_quotes = [q for q in quotes if q.total_price is not None]
    if valid_quotes:
        return min(valid_quotes, key=lambda q: q.total_price)
    return quotes[0] if quotes else None


def summarize_quotes(quotes: List[QuoteCandidate]) -> str:
    if not quotes:
        return "No quote candidates extracted."

    lines = []
    for quote in quotes[:10]:
        rate_type = "AAA" if quote.aaa_applied else "Standard"
        total = quote.total_text or "Unavailable"
        lines.append(
            f"- {total} | {rate_type} | {quote.pickup_location} | "
            f"{quote.pickup_date} to {quote.return_date} | "
            f"{quote.vehicle_summary or 'Vehicle unavailable'}"
        )
    return "\n".join(lines)


def send_email(subject: str, html_body: str, plain_body: str) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["ALERT_RECIPIENT"]

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(plain_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, recipient, message.as_string())


class HertzMonitor:
    def __init__(self) -> None:
        self.target_total = float(os.getenv("TARGET_TOTAL_USD", str(DEFAULT_TARGET_TOTAL)))
        self.locations = env_json("HERTZ_LOCATION_CANDIDATES", DEFAULT_LOCATION_CANDIDATES)
        self.date_offsets = env_json("HERTZ_DATE_OFFSETS", DEFAULT_DATE_OFFSETS)
        self.rental_lengths = env_json("HERTZ_RENTAL_LENGTHS", DEFAULT_RENTAL_LENGTHS)
        self.send_if_no_quotes = env_bool("SEND_EMAIL_IF_NO_QUOTES", True)
        self.capture_debug = env_bool("CAPTURE_DEBUG_ARTIFACTS", True)
        self.aaa_offer = fetch_aaa_offer_details()
        self.generated_at = datetime.now()

    def run(self) -> None:
        ensure_artifacts()
        quotes: List[QuoteCandidate] = []
        errors: List[str] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()

            try:
                for location in self.locations:
                    for offset in self.date_offsets:
                        for length in self.rental_lengths:
                            try:
                                quote = self.collect_quote(page, location, offset, length)
                                if quote:
                                    quotes.append(quote)
                            except Exception as exc:
                                errors.append(f"{location} | +{offset}d | {length}d: {exc}")
                                if self.capture_debug:
                                    self.save_snapshot(
                                        page,
                                        f"error-{self.safe_name(location)}-{offset}-{length}",
                                    )
            finally:
                browser.close()

        best_quote = pick_best_quote(quotes)
        self.write_summary(best_quote, quotes, errors)

        if best_quote or self.send_if_no_quotes:
            subject, html_body, plain_body = self.build_email(best_quote, quotes, errors)
            send_email(subject, html_body, plain_body)

    def collect_quote(self, page, location: str, offset_days: int, rental_length: int) -> Optional[QuoteCandidate]:
        pickup_date = date.today() + timedelta(days=offset_days)
        return_date = pickup_date + timedelta(days=rental_length)

        self.open_booking_page(page, location, offset_days, rental_length)
        self.fill_location(page, location)
        self.set_dates(page, pickup_date, return_date)
        self.submit_search(page)
        return self.extract_quote(page, location, pickup_date, return_date)

    def open_booking_page(self, page, location: str, offset_days: int, rental_length: int) -> None:
        last_error: Optional[Exception] = None

        for url in HERTZ_BOOKING_URLS:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)
                self.close_cookie_banner(page)
                self.dismiss_common_modals(page)
                self.wait_for_booking_form(page)
                return
            except Exception as exc:
                last_error = exc
                if self.capture_debug:
                    self.save_snapshot(
                        page,
                        f"bootstrap-{self.safe_name(location)}-{offset_days}-{rental_length}-{self.safe_name(url)}",
                    )

        raise RuntimeError(f"Could not reach Hertz booking form. Last error: {last_error}")

    def close_cookie_banner(self, page) -> None:
        selectors = [
            "button[aria-label='Close']",
            "button:has-text('Close')",
            "button:has-text('Accept')",
            "button:has-text('Accept All')",
            "button:has-text('I Agree')",
            "button:has-text('Continue')",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() >= 1 and locator.first.is_visible():
                    locator.first.click(timeout=2500)
                    page.wait_for_timeout(400)
            except Exception:
                continue

    def dismiss_common_modals(self, page) -> None:
        selectors = [
            "[data-testid='close-button']",
            "dialog button",
            ".modal button",
            "[role='dialog'] button",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() >= 1 and locator.first.is_visible():
                    text = (locator.first.inner_text(timeout=500) or "").strip().lower()
                    if text in {"close", "x", "dismiss"} or selector != "dialog button":
                        locator.first.click(timeout=2000)
                        page.wait_for_timeout(300)
            except Exception:
                continue

    def wait_for_booking_form(self, page) -> None:
        selectors = [
            "#locationInput",
            "input[placeholder='Select your location']",
            "input[role='combobox']",
            "#submitButton",
            "text=Book your Hertz car rental",
            "text=View vehicles",
        ]

        for _ in range(30):
            self.close_cookie_banner(page)
            self.dismiss_common_modals(page)

            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    if locator.count() >= 1 and locator.first.is_visible():
                        return
                except Exception:
                    continue

            page.wait_for_timeout(1000)

        excerpt = ""
        try:
            excerpt = page.locator("body").inner_text(timeout=5000)[:500]
        except Exception:
            pass
        raise RuntimeError(f"Booking form never became visible at {page.url}. Body excerpt: {excerpt!r}")

    def find_location_field(self, page):
        selectors = [
            "#locationInput",
            "input[placeholder='Select your location']",
            "input[role='combobox']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() >= 1 and locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
        raise RuntimeError(f"Could not find location input on {page.url}")

    def fill_location(self, page, location: str) -> None:
        field = self.find_location_field(page)
        field.fill(location)
        page.wait_for_timeout(1200)
        field.press("ArrowDown")
        page.wait_for_timeout(250)
        field.press("Enter")
        page.wait_for_timeout(1200)

    def find_date_trigger(self, page):
        selectors = [
            "#dateTimePickerTriggerFrom",
            "input[aria-label='PickUpLocationSelect']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() >= 1 and locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
        raise RuntimeError(f"Could not find pickup date trigger on {page.url}")

    def click_calendar_date(self, page, target_date: date) -> None:
        labels = [
            target_date.strftime("%a %b %d %Y"),
            target_date.strftime("%A %b %d %Y"),
        ]
        last_error: Optional[Exception] = None
        for label in labels:
            try:
                page.get_by_label(label).click(timeout=8000)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not click calendar date {target_date.isoformat()}: {last_error}")

    def set_dates(self, page, pickup_date: date, return_date: date) -> None:
        trigger = self.find_date_trigger(page)
        trigger.click(timeout=10000)
        self.click_calendar_date(page, pickup_date)
        self.click_calendar_date(page, return_date)
        page.get_by_role("button", name="Apply").click(timeout=10000)
        page.wait_for_timeout(500)

    def submit_search(self, page) -> None:
        selectors = [
            "#submitButton",
            "button[type='submit']",
            "button:has-text('View vehicles')",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() >= 1 and locator.first.is_visible():
                    locator.first.click(timeout=15000)
                    page.wait_for_timeout(8000)
                    return
            except Exception:
                continue
        raise RuntimeError(f"Could not find submit button on {page.url}")

    def extract_quote(
        self,
        page,
        location: str,
        pickup_date: date,
        return_date: date,
    ) -> Optional[QuoteCandidate]:
        text = page.locator("body").inner_text(timeout=15000)
        compact = re.sub(r"\s+", " ", text)

        vehicle_match = re.search(
            r"(Economy|Compact|Midsize|Intermediate|Standard|Full[- ]?Size|Premium|SUV|Minivan)[^\n]{0,80}",
            compact,
            re.I,
        )
        price_texts = re.findall(r"\$[0-9,]+(?:\.[0-9]{2})?", compact)
        prices = [parse_price(value) for value in price_texts]
        prices = [p for p in prices if p is not None and 100 <= p <= 5000]
        best_price = min(prices) if prices else None

        aaa_applied = "AAA" in compact.upper()
        booking_url = page.url
        notes = ""
        if booking_url in HERTZ_BOOKING_URLS:
            notes = "Search stayed on a Hertz entry page; quote may be partial."

        if best_price is None and vehicle_match is None and not notes:
            return None

        total_text = f"${best_price:,.2f}" if best_price is not None else ""
        quote = QuoteCandidate(
            pickup_location=location,
            pickup_date=pickup_date.isoformat(),
            return_date=return_date.isoformat(),
            aaa_applied=aaa_applied,
            total_price=best_price,
            total_text=total_text,
            vehicle_summary=vehicle_match.group(0).strip() if vehicle_match else "",
            booking_url=booking_url,
            notes=notes,
        )

        if self.capture_debug:
            self.save_snapshot(
                page,
                f"quote-{self.safe_name(location)}-{pickup_date.isoformat()}-{rental_length_days(pickup_date, return_date)}",
            )

        return quote

    def build_email(
        self,
        best_quote: Optional[QuoteCandidate],
        quotes: List[QuoteCandidate],
        errors: List[str],
    ) -> tuple[str, str, str]:
        day_text = self.generated_at.strftime("%B %d, %Y")

        if best_quote and best_quote.total_price is not None:
            subject = f"Hertz/AAA monitor {day_text}: best found ${best_quote.total_price:,.2f}"
        else:
            subject = f"Hertz/AAA monitor {day_text}: no firm quote extracted"

        aaa_context = (
            f"AAA offer page: {self.aaa_offer.get('offer_url', AAA_OFFER_URL)}\n"
            f"Book by: {self.aaa_offer.get('book_by', 'Unknown')}"
        )

        if best_quote:
            rate_type = "AAA pricing" if best_quote.aaa_applied else "standard Hertz pricing"
            best_total = best_quote.total_text or "Unavailable"
            target_note = (
                "Target met or beaten."
                if best_quote.total_price is not None and best_quote.total_price <= self.target_total
                else "Still above your $700 target."
            )
            html_body = f"""
            <h1>Hertz + AAA Daily Rental Monitor</h1>
            <p>{day_text}</p>
            <p><strong>{target_note}</strong></p>
            <h2>Best Current Option</h2>
            <ul>
              <li><strong>Total:</strong> {best_total}</li>
              <li><strong>Rate type:</strong> {rate_type}</li>
              <li><strong>Pickup:</strong> {best_quote.pickup_location}</li>
              <li><strong>Dates:</strong> {best_quote.pickup_date} to {best_quote.return_date}</li>
              <li><strong>Vehicle:</strong> {best_quote.vehicle_summary or "Unavailable"}</li>
              <li><strong>Booking URL:</strong> <a href="{best_quote.booking_url}">{best_quote.booking_url}</a></li>
              <li><strong>Notes:</strong> {best_quote.notes or "None"}</li>
            </ul>
            <h2>AAA Offer Context</h2>
            <pre>{aaa_context}</pre>
            <h2>Other Candidate Quotes</h2>
            <pre>{summarize_quotes(quotes)}</pre>
            <h2>Errors</h2>
            <pre>{chr(10).join(errors[:20]) if errors else "None"}</pre>
            """
            plain_body = (
                f"Hertz + AAA Daily Rental Monitor\n{day_text}\n\n"
                f"Best current option: {best_total}\n"
                f"Rate type: {rate_type}\n"
                f"Pickup: {best_quote.pickup_location}\n"
                f"Dates: {best_quote.pickup_date} to {best_quote.return_date}\n"
                f"Vehicle: {best_quote.vehicle_summary or 'Unavailable'}\n"
                f"Booking URL: {best_quote.booking_url}\n"
                f"Notes: {best_quote.notes or 'None'}\n\n"
                f"{aaa_context}\n\n"
                f"Other candidates:\n{summarize_quotes(quotes)}\n\n"
                f"Errors:\n{chr(10).join(errors[:20]) if errors else 'None'}\n"
            )
            return subject, html_body, plain_body

        html_body = f"""
        <h1>Hertz + AAA Daily Rental Monitor</h1>
        <p>{day_text}</p>
        <p>No firm total was extracted today.</p>
        <h2>AAA Offer Context</h2>
        <pre>{aaa_context}</pre>
        <h2>Candidate Quotes</h2>
        <pre>{summarize_quotes(quotes)}</pre>
        <h2>Errors</h2>
        <pre>{chr(10).join(errors[:20]) if errors else "None"}</pre>
        """
        plain_body = (
            f"Hertz + AAA Daily Rental Monitor\n{day_text}\n\n"
            "No firm total was extracted today.\n\n"
            f"{aaa_context}\n\n"
            f"Candidate quotes:\n{summarize_quotes(quotes)}\n\n"
            f"Errors:\n{chr(10).join(errors[:20]) if errors else 'None'}\n"
        )
        return subject, html_body, plain_body

    def write_summary(
        self,
        best_quote: Optional[QuoteCandidate],
        quotes: List[QuoteCandidate],
        errors: List[str],
    ) -> None:
        ensure_artifacts()
        payload = {
            "generated_at": self.generated_at.isoformat(),
            "target_total_usd": self.target_total,
            "aaa_offer": self.aaa_offer,
            "best_quote": asdict(best_quote) if best_quote else None,
            "quotes": [asdict(quote) for quote in quotes],
            "errors": errors,
        }
        (ARTIFACT_DIR / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def save_snapshot(self, page, stem: str) -> None:
        ensure_artifacts()
        (ARTIFACT_DIR / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(ARTIFACT_DIR / f"{stem}.png"), full_page=True)

    @staticmethod
    def safe_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()


def rental_length_days(pickup_date: date, return_date: date) -> int:
    return (return_date - pickup_date).days


if __name__ == "__main__":
    HertzMonitor().run()
