"""
Booking webhook for the My Escape Nail Spa Retell AI receptionist.

Exposes three endpoints that a Retell custom function calls during a live phone call:
  POST /check_availability
  POST /book_appointment
  POST /take_message

check_availability and book_appointment drive a real Chromium browser (via Playwright)
through go-booking.gocheckin.net, because GoCheckIn has no public API. take_message just
sends a notification (fill in your own SMS/email provider) -- no browser automation needed.

--------------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------------
1. pip install flask playwright
2. playwright install chromium
3. Set environment variables:
     GOCHECKIN_EMAIL=you@example.com
     GOCHECKIN_PASSWORD=your-password
4. Fill in the two TODOs inside login() below -- see the comment there for how to find
   the correct selectors. I could not capture these myself because the browser session
   I inspected was already logged in.
5. Run locally to test:  python booking_webhook.py
   Then deploy somewhere reachable from the internet (Render / Railway / Fly.io / a VPS)
   and point the function URLs in retell_functions.json at that deployment.
--------------------------------------------------------------------------------------
"""

import os
import re
from datetime import datetime

from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

GOCHECKIN_URL = "https://go-booking.gocheckin.net/"
GOCHECKIN_EMAIL = os.environ.get("GOCHECKIN_EMAIL")
GOCHECKIN_PASSWORD = os.environ.get("GOCHECKIN_PASSWORD")

app = Flask(__name__)


def login(page):
    """
    Log into Go Booking. TODO before first run:
    1. Open https://go-booking.gocheckin.net/ yourself in a normal browser (logged out).
    2. Right-click the email field -> Inspect. Note its 'placeholder' or 'name' attribute.
    3. Do the same for the password field and the submit/login button.
    4. Replace the placeholder selectors below with the real ones.
    """
    page.goto(GOCHECKIN_URL)

    # TODO: replace with the real selector you find in step 2 above, e.g.:
    # page.fill('input[placeholder="Email"]', GOCHECKIN_EMAIL)
    page.fill('input[type="email"]', GOCHECKIN_EMAIL)

    # TODO: replace with the real selector for the password field
    page.fill('input[type="password"]', GOCHECKIN_PASSWORD)

    # TODO: replace with the real login button selector/text
    page.get_by_role("button", name=re.compile("log ?in", re.I)).click()

    page.wait_for_load_state("networkidle")


def open_new_appointment_modal(page, date_str):
    """Navigate the calendar to the requested date and open the New Appointment modal."""
    # Use the datepicker input at the top of the calendar to jump to the requested date.
    page.get_by_role("button", name="New").click()
    page.wait_for_selector("text=New Appointment")

    target = datetime.strptime(date_str, "%Y-%m-%d")
    # The modal shows a week strip (sun..sat) with day numbers; click the matching day.
    page.get_by_text(str(target.day), exact=True).first.click()


def find_or_create_client(page, client_name, phone_number):
    search_box = page.get_by_placeholder("Search Name/Phone ...")
    search_box.fill(phone_number)
    page.wait_for_timeout(800)  # allow client search results to load

    existing = page.locator(f"text={phone_number}")
    if existing.count() > 0:
        existing.first.click()
    else:
        page.get_by_role("button", name="+ Create new client").click()
        # TODO: verify the new-client form field selectors the first time you run this --
        # I did not capture the "create new client" sub-form since I stopped short of
        # creating a test client in your live account.
        page.get_by_placeholder("Name").fill(client_name)
        page.get_by_placeholder("Phone").fill(phone_number)
        page.get_by_role("button", name=re.compile("save|create", re.I)).click()


def fill_appointment_details(page, start_time, duration_minutes, staff, service):
    page.get_by_label("Start time").fill(start_time)

    hours, minutes = divmod(int(duration_minutes), 60)
    page.get_by_label("Duration").fill(f"{hours}h {minutes}m")

    if staff and staff.lower() != "any":
        page.get_by_label("Staff").click()
        page.get_by_text(staff, exact=False).click()

    page.get_by_label("Service").fill(service)
    page.wait_for_timeout(500)
    # If GoCheckIn shows an autocomplete dropdown of matching services, pick the first match.
    suggestion = page.locator(".service-suggestion, [role='option']").first
    if suggestion.count() > 0:
        suggestion.click()


@app.route("/check_availability", methods=["POST"])
def check_availability():
    data = request.json or {}
    date_str = data.get("date")
    service = data.get("service")
    staff_preference = data.get("staff_preference", "any")

    if not date_str:
        return jsonify({"error": "date is required"}), 400

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login(page)
            page.goto(GOCHECKIN_URL)
            page.wait_for_load_state("networkidle")

            # Read the calendar grid for the requested date and collect free 30-min slots
            # per staff column. This depends on GoCheckIn's DOM structure -- inspect the
            # calendar in devtools and adjust the selector below if it doesn't match.
            booked_blocks = page.locator("[class*='appointment'], [class*='event']").all_inner_texts()

            # NOTE: this is a starting point, not a finished slot calculator. A simple and
            # reliable alternative: return the salon's normal business hours in 30-minute
            # increments minus any time ranges you can parse out of booked_blocks, and let
            # the agent double check available slots verbally against Retell's own logic,
            # or just always offer 3 slots (e.g. next opening, +2h, +4h) and let
            # book_appointment fail gracefully with a clear message if the slot is taken.
            open_slots = ["10:00 AM", "1:00 PM", "4:00 PM"]  # placeholder until wired up

            return jsonify({
                "date": date_str,
                "service": service,
                "staff_preference": staff_preference,
                "available_slots": open_slots,
            })
        finally:
            browser.close()


@app.route("/book_appointment", methods=["POST"])
def book_appointment():
    data = request.json or {}
    required = ["client_name", "phone_number", "service", "staff", "date", "start_time", "duration_minutes"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login(page)
            page.goto(GOCHECKIN_URL)
            page.wait_for_load_state("networkidle")

            open_new_appointment_modal(page, data["date"])
            find_or_create_client(page, data["client_name"], data["phone_number"])
            fill_appointment_details(
                page,
                data["start_time"],
                data["duration_minutes"],
                data.get("staff", "any"),
                data["service"],
            )

            if data.get("notes"):
                page.get_by_placeholder("Appointment Note").fill(data["notes"])

            page.get_by_role("button", name="Book").click()
            page.wait_for_timeout(1500)

            # TODO: check for a real success indicator (e.g. modal closing, a toast message)
            # instead of just assuming success after the click.
            success = True

            # Notify David so he can mirror the booking on SmartScheduling if he wants to.
            notify_owner(data)

            return jsonify({"success": success, "confirmed": data})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            browser.close()


@app.route("/take_message", methods=["POST"])
def take_message():
    data = request.json or {}
    # TODO: wire this up to a real SMS/email provider (Twilio, SendGrid, etc.) so David
    # actually receives it. For now this just logs to stdout.
    print("NEW MESSAGE FOR STAFF FOLLOW-UP:", data)
    return jsonify({"received": True})


def notify_owner(booking):
    """
    TODO: send David a text or email with the booking details so he can manually
    mirror it into smartscheduling.com/calendar if he's keeping that in sync.
    Example with Twilio (after `pip install twilio` and setting TWILIO_* env vars):

        from twilio.rest import Client
        client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
        client.messages.create(
            to="+19094470628",
            from_=os.environ["TWILIO_FROM"],
            body=f"New booking: {booking['client_name']} - {booking['service']} "
                 f"on {booking['date']} at {booking['start_time']} with {booking['staff']}",
        )
    """
    print("New booking to mirror on SmartScheduling if desired:", booking)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
