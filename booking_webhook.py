"""
Booking webhook for the My Escape Nail Spa Retell AI receptionist.

Exposes three endpoints that a Retell custom function calls during a live phone call:
  POST /check_availability
  POST /book_appointment
  POST /take_message

Call flow that matches how David actually runs the salon:
  1. check_availability  -> reads SmartScheduling's calendar (David's source of truth for
     what's already on the books) to see what's open on the requested date.
  2. book_appointment     -> books the real appointment on GoCheckIn, keyed off the caller's
     phone number, then writes the same appointment onto SmartScheduling's calendar so David
     can see it too.
  3. take_message         -> logs a note for staff follow-up (cancellations, complaints, etc).

Neither GoCheckIn nor SmartScheduling has a public API, so both are automated with a real
Chromium browser (via Playwright) clicking through the same screens a staff member would use.

--------------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------------
1. pip install flask playwright
2. playwright install chromium
3. Set environment variables:
     GOCHECKIN_EMAIL=you@example.com
     GOCHECKIN_PASSWORD=your-password
     SMARTSCHEDULING_EMAIL=you@example.com
     SMARTSCHEDULING_PASSWORD=your-password
4. Fill in the TODOs inside login() and login_smartscheduling() below -- see the comments
   for how to find the correct selectors. I could not capture these myself because the
   browser sessions I inspected were already logged in.
5. Run locally to test:  python booking_webhook.py
   Then deploy somewhere reachable from the internet (Render / Railway / Fly.io / a VPS)
   and point the function URLs in Retell at that deployment.
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

SMARTSCHEDULING_URL = "https://smartscheduling.com/calendar"
SMARTSCHEDULING_LOGIN_URL = "https://smartscheduling.com/en/account/login"
SMARTSCHEDULING_EMAIL = os.environ.get("SMARTSCHEDULING_EMAIL")
SMARTSCHEDULING_PASSWORD = os.environ.get("SMARTSCHEDULING_PASSWORD")

DEFAULT_DURATION_MINUTES = 60
DEFAULT_STAFF = "any"

app = Flask(__name__)


# --------------------------------------------------------------------------------------
# GoCheckIn (go-booking.gocheckin.net) -- the real system customers are booked into
# --------------------------------------------------------------------------------------

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


def book_on_gocheckin(data):
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
                data.get("staff", DEFAULT_STAFF),
                data["service"],
            )

            if data.get("notes"):
                page.get_by_placeholder("Appointment Note").fill(data["notes"])

            page.get_by_role("button", name="Book").click()
            page.wait_for_timeout(1500)

            # TODO: check for a real success indicator (e.g. modal closing, a toast message)
            # instead of just assuming success after the click.
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            browser.close()


# --------------------------------------------------------------------------------------
# SmartScheduling (smartscheduling.com/calendar) -- David's personal calendar view.
# Its public online-booking widget is disabled, so this mirrors the same appointment
# through the internal calendar the same way a staff member would create it.
# --------------------------------------------------------------------------------------

def login_smartscheduling(page):
    """
    Log into SmartScheduling. TODO before first run:
    1. Open https://smartscheduling.com/en/account/login yourself in a normal browser
       (logged out).
    2. Right-click the email/username field -> Inspect. Note its selector.
    3. Do the same for the password field and the submit/login button.
    4. Replace the placeholder selectors below with the real ones.
    """
    page.goto(SMARTSCHEDULING_LOGIN_URL)
    page.fill('#UserName', SMARTSCHEDULING_EMAIL)
    page.fill('#Password', SMARTSCHEDULING_PASSWORD)
    page.get_by_role("button", name=re.compile("sign ?in", re.I)).click()
    page.wait_for_load_state("networkidle")
    print(f"[smartscheduling] after login, landed on: {page.url}")


def goto_smartscheduling_date(page, target):
    """
    SmartScheduling's calendar always loads on today's date. To view another day, click the
    "Tue, 07/21/2026"-style button (id="display-current-date-button") in the top-left to open
    a small month-grid date picker, then click the plain-text day-number cell for the target
    date. No-op if the target date is already today (the default view).
    """
    today = datetime.now().date()
    print(f"[smartscheduling] goto_date: target={target.date()} server_today={today} url={page.url}")
    if target.date() == today:
        return

    page.click("#display-current-date-button")
    page.wait_for_timeout(300)
    day_cell = page.get_by_text(str(target.day), exact=True).first
    day_cell.click()
    page.wait_for_timeout(800)


def create_smartscheduling_appointment(page, data):
    """
    Open the calendar, click any empty grid cell to open the "New Appointment" modal
    (staff/date/time picked there are only approximate -- the modal's own fields are
    used afterward to set the precise values), then fill in and save.
    """
    page.goto(SMARTSCHEDULING_URL)
    page.wait_for_load_state("networkidle")

    # Navigate the calendar to the requested date BEFORE opening the modal, so whatever
    # date the modal defaults to (today, or the grid cell clicked) is already correct.
    target = datetime.strptime(data["date"], "%Y-%m-%d")
    goto_smartscheduling_date(page, target)

    # Click into an empty area of the calendar grid to open the New Appointment modal.
    # TODO: if this misses and hits an existing appointment block instead, adjust the
    # coordinate or switch to a specific staff column first via the "All staff" dropdown.
    page.mouse.click(700, 500)
    page.wait_for_selector("text=Appointment", timeout=5000)

    # Services: click "+" and select/search the matching service.
    page.get_by_text("+", exact=True).first.click()
    page.wait_for_timeout(500)
    search = page.get_by_placeholder(re.compile("search", re.I))
    if search.count() > 0:
        search.first.fill(data["service"])
        page.wait_for_timeout(500)
        page.get_by_text(data["service"], exact=False).first.click()

    # Staff member.
    staff = data.get("staff", DEFAULT_STAFF)
    if staff and staff.lower() != "any":
        page.get_by_label("Staff Member").click()
        page.get_by_text(staff, exact=False).click()

    # Name / phone.
    name_parts = data["client_name"].split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    page.get_by_label("First Name").fill(first_name)
    if last_name:
        page.get_by_label("Last Name").fill(last_name)
    page.get_by_label("Phone").fill(data["phone_number"])

    # Start / finish time.
    page.get_by_label("Start").click()
    page.get_by_text(data["start_time"], exact=False).first.click()
    if data.get("finish_time"):
        page.get_by_label("Finish").click()
        page.get_by_text(data["finish_time"], exact=False).first.click()

    if data.get("notes"):
        page.get_by_label("Notes").fill(data["notes"])

    page.get_by_role("button", name="Save").click()
    page.wait_for_timeout(1500)


def mirror_on_smartscheduling(data):
    """Best-effort mirror -- failures here are logged but never fail the caller's booking,
    since GoCheckIn is the system of record."""
    if not (SMARTSCHEDULING_EMAIL and SMARTSCHEDULING_PASSWORD):
        print("SmartScheduling mirror skipped: SMARTSCHEDULING_EMAIL/PASSWORD not set")
        return {"attempted": False}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login_smartscheduling(page)
            create_smartscheduling_appointment(page, data)
            return {"attempted": True, "success": True}
        except Exception as e:
            print("SmartScheduling mirror failed:", e)
            return {"attempted": True, "success": False, "error": str(e)}
        finally:
            browser.close()


BUSINESS_HOURS = {
    # weekday() -> (open_hour, close_hour), 24h clock. Monday=0 ... Sunday=6.
    1: (10, 19), 2: (10, 19), 3: (10, 19), 4: (10, 19), 5: (10, 19),  # Tue-Sat
    6: (10, 17),  # Sunday
    # Monday (0) is closed -- left out on purpose.
}


def _time_to_hour(hour_str, minute_str, meridiem):
    hour = int(hour_str) % 12
    if meridiem.upper() == "PM":
        hour += 12
    return hour


def read_smartscheduling_availability(page, date_str):
    """
    Navigate SmartScheduling's calendar to the requested date and read back which slots
    are already booked, so we can offer only genuinely open times.

    Since the salon books "anyone available" rather than a specific tech by default, an hour
    only counts as fully booked once EVERY staff column has an overlapping appointment --
    one busy tech shouldn't hide a slot that another tech could still take.
    """
    page.goto(SMARTSCHEDULING_URL)
    page.wait_for_load_state("networkidle")

    target = datetime.strptime(date_str, "%Y-%m-%d")
    goto_smartscheduling_date(page, target)

    staff_count = page.locator(".dhx_scale_bar").count() or 1

    # Collect the appointment blocks on the visible calendar. This is a DHTMLX Scheduler
    # instance -- each appointment renders as a div.dhx_cal_event, with text like
    # "10:00 am - 11:00 amRose with Chloe - Acrylic Fill S/M".
    booked_texts = page.locator(".dhx_cal_event").all_inner_texts()
    busy_count_by_hour = {}
    time_range_re = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)\s*-\s*(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
    for text in booked_texts:
        match = time_range_re.search(text)
        if not match:
            continue
        start_hour = _time_to_hour(match.group(1), match.group(2), match.group(3))
        end_hour = _time_to_hour(match.group(4), match.group(5), match.group(6))
        # If the appointment ends exactly on the hour (e.g. ends at 11:00), that hour itself
        # isn't occupied; otherwise round up so a partial-hour appointment still blocks it.
        end_minute = int(match.group(5))
        last_hour = end_hour if end_minute == 0 else end_hour + 1
        for hour in range(start_hour, last_hour):
            busy_count_by_hour[hour] = busy_count_by_hour.get(hour, 0) + 1

    weekday = target.weekday()
    if weekday not in BUSINESS_HOURS:
        return []  # closed that day

    open_hour, close_hour = BUSINESS_HOURS[weekday]
    open_slots = []
    for hour in range(open_hour, close_hour):
        if busy_count_by_hour.get(hour, 0) < staff_count:
            suffix = "AM" if hour < 12 else "PM"
            display_hour = hour if hour <= 12 else hour - 12
            display_hour = 12 if display_hour == 0 else display_hour
            open_slots.append(f"{display_hour}:00 {suffix}")

    return open_slots


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route("/check_availability", methods=["POST"])
def check_availability():
    data = request.json or {}
    date_str = data.get("date")
    service = data.get("service")
    staff_preference = data.get("staff_preference", DEFAULT_STAFF)

    if not date_str:
        return jsonify({"error": "date is required"}), 400

    if not (SMARTSCHEDULING_EMAIL and SMARTSCHEDULING_PASSWORD):
        return jsonify({"error": "SmartScheduling credentials not configured"}), 500

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login_smartscheduling(page)
            open_slots = read_smartscheduling_availability(page, date_str)

            return jsonify({
                "date": date_str,
                "service": service,
                "staff_preference": staff_preference,
                "available_slots": open_slots,
            })
        except Exception as e:
            # Debug info to see where the browser actually ended up when it failed --
            # this is temporary while we're diagnosing live test failures.
            debug = {}
            try:
                debug = {"page_url": page.url, "page_title": page.title()}
            except Exception:
                pass
            return jsonify({"error": str(e), "debug": debug}), 500
        finally:
            browser.close()


@app.route("/book_appointment", methods=["POST"])
def book_appointment():
    data = dict(request.json or {})

    # Only these are truly essential -- everything else gets a sensible default so the
    # agent doesn't fail a booking just because the conversation didn't explicitly collect
    # a duration or a staff preference.
    required = ["client_name", "phone_number", "service", "date", "start_time"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    data.setdefault("staff", DEFAULT_STAFF)
    data.setdefault("duration_minutes", DEFAULT_DURATION_MINUTES)

    gocheckin_result = book_on_gocheckin(data)
    if not gocheckin_result.get("success"):
        return jsonify({"success": False, "error": gocheckin_result.get("error")}), 500

    smartscheduling_result = mirror_on_smartscheduling(data)

    return jsonify({
        "success": True,
        "confirmed": data,
        "smartscheduling": smartscheduling_result,
    })


@app.route("/take_message", methods=["POST"])
def take_message():
    data = request.json or {}
    # TODO: wire this up to a real SMS/email provider (Twilio, SendGrid, etc.) so David
    # actually receives it. For now this just logs to stdout.
    print("NEW MESSAGE FOR STAFF FOLLOW-UP:", data)
    return jsonify({"received": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
