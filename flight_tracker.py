import os
import time
import json
import requests
import gspread
import argparse
from collections import defaultdict
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Setup Google Sheets
GOOGLE_SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')
GOOGLE_CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS')

gc = None
if GOOGLE_CREDENTIALS_JSON:
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = Credentials.from_service_account_info(
            creds_dict, 
            scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        )
        gc = gspread.authorize(credentials)
        print("Successfully authenticated with Google Sheets.")
    except Exception as e:
        print(f"Failed to authenticate with Google Sheets: {e}")

# Setup Amadeus Request Variables
AMADEUS_CLIENT_ID = os.environ.get('AMADEUS_CLIENT_ID', 'your_client_id')
AMADEUS_CLIENT_SECRET = os.environ.get('AMADEUS_CLIENT_SECRET', 'your_client_secret')
AMADEUS_TOKEN_URL = 'https://test.api.amadeus.com/v1/security/oauth2/token'
AMADEUS_SEARCH_URL = 'https://test.api.amadeus.com/v2/shopping/flight-offers'

def get_amadeus_token():
    try:
        data = {
            'grant_type': 'client_credentials',
            'client_id': AMADEUS_CLIENT_ID,
            'client_secret': AMADEUS_CLIENT_SECRET
        }
        response = requests.post(AMADEUS_TOKEN_URL, data=data)
        response.raise_for_status()
        return response.json()['access_token']
    except Exception as e:
        print(f"Failed to get Amadeus access token: {e}")
        return None

access_token = get_amadeus_token()

DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')
# Configurable price threshold for discord, default $80.
DISCORD_PRICE_THRESHOLD = float(os.environ.get('DISCORD_PRICE_THRESHOLD', '80.0'))

BAY_AREA_AIRPORTS = ['SFO', 'SJC', 'OAK']
LA_AIRPORTS = ['LAX']

def get_flight_dates(start_date, days_ahead=180):
    end_date = start_date + timedelta(days=days_ahead)
    current_date = start_date
    dates = set()
    
    while current_date <= end_date:
        if current_date.weekday() == 4: # Friday
            # Friday to Sunday
            dates.add((current_date.strftime('%Y-%m-%d'), (current_date + timedelta(days=2)).strftime('%Y-%m-%d')))
            # Friday to Monday (extended)
            dates.add((current_date.strftime('%Y-%m-%d'), (current_date + timedelta(days=3)).strftime('%Y-%m-%d')))
        elif current_date.weekday() == 5: # Saturday
            # Saturday to Sunday
            dates.add((current_date.strftime('%Y-%m-%d'), (current_date + timedelta(days=1)).strftime('%Y-%m-%d')))
            # Saturday to Monday (extended)
            dates.add((current_date.strftime('%Y-%m-%d'), (current_date + timedelta(days=2)).strftime('%Y-%m-%d')))
        elif current_date.weekday() == 3: # Thursday
            # Thursday to Sunday (extended)
            dates.add((current_date.strftime('%Y-%m-%d'), (current_date + timedelta(days=3)).strftime('%Y-%m-%d')))
            
        current_date += timedelta(days=1)
        
    return sorted(list(dates))

def fetch_cheapest_flight(origin, destination, dep_date, ret_date=None, retries=3):
    if not access_token:
        return None
        
    for attempt in range(retries):
        try:
            headers = {'Authorization': f'Bearer {access_token}'}
            params = {
                'originLocationCode': origin,
                'destinationLocationCode': destination,
                'departureDate': dep_date,
                'adults': 1,
                'currencyCode': 'USD',
                'max': 1
            }
            if ret_date:
                params['returnDate'] = ret_date
                
            res = requests.get(AMADEUS_SEARCH_URL, headers=headers, params=params)
            
            # If successful, parse and return the price
            if res.status_code == 200:
                data = res.json().get('data', [])
                if data:
                    return float(data[0]['price']['total'])
                return None
            
            # 429: Rate Limit or Quota Exceeded
            if res.status_code == 429:
                err_text = res.text.lower()
                # Check if it's a hard monthly quota limit
                if "quota" in err_text or "exceeded" in err_text:
                    print(f"\n[CRITICAL ERROR] Amadeus Monthly Quota Exceeded! Terminating script to prevent further errors.")
                    print(f"Server Response: {res.text}")
                    exit(1)
                
                # Otherwise, it's a speed limit (Too Many Requests). Apply exponential backoff.
                wait_time = (2 ** attempt) + 1  # 2s, 3s, 5s...
                print(f"[WARNING] Rate limit hit for {origin}->{destination}. Retrying in {wait_time}s... (Attempt {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
                
            # 401 or 403: Unauthorized / Forbidden
            if res.status_code in (401, 403):
                print(f"\n[CRITICAL ERROR] Amadeus Authentication Failed (401/403). Check your API keys!")
                print(f"Server Response: {res.text}")
                exit(1)
                
            # Handle other expected API errors (e.g. no flights found for that specific date)
            res.raise_for_status()
            
        except requests.exceptions.RequestException as e:
            # If it's a generic connection error or a non-429/401 HTTP error we want to swallow it but log it visually
            print(f"[API ERROR] {origin}->{destination} on {dep_date}: {e}")
            break # Break out of retry loop for standard errors (don't hammer the server)
            
    return None

def process_results(all_results, skip_sheets=False, threshold=80.0):
    # --- 1. Google Sheets Logic (Calendar Format) ---
    if not skip_sheets and gc and GOOGLE_SHEET_ID:
        try:
            sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            
            # Group by Month: e.g. "2026-04"
            flights_by_month = defaultdict(list)
            for f in all_results:
                month_key = f['dep_date'][:7] # YYYY-MM
                flights_by_month[month_key].append(f)
                
            # Prepare rows
            rows = [["Month", "Option 1", "Option 2", "Option 3", "Option 4", "Option 5"]]
            
            for month in sorted(flights_by_month.keys()):
                # Sort flights in this month by price ascending
                month_flights = sorted(flights_by_month[month], key=lambda x: x['price'])
                
                # Take top 5 unique cheapest weekends (avoiding exact duplicates if possible)
                top_5 = []
                seen_dates = set()
                for f in month_flights:
                    date_pair = (f['dep_date'], f['ret_date'])
                    if date_pair not in seen_dates:
                        seen_dates.add(date_pair)
                        top_5.append(f)
                    if len(top_5) == 5:
                        break
                        
                row_data = [month]
                for f in top_5:
                    anniv = " 🎉" if f.get('is_anniv') else ""
                    cell_text = f"{f['origin']}->{f['destination']}\n{f['dep_date']} to {f['ret_date']}\n${f['price']:.2f}{anniv}"
                    row_data.append(cell_text)
                    
                # Pad to 6 columns total if less than 5 flights found
                while len(row_data) < 6:
                    row_data.append("")
                    
                rows.append(row_data)
                
            sheet.clear()
            # Make columns a bit wider to handle multiline text
            sheet.update(f"A1:F{len(rows)}", rows)
            sheet.format('A1:F1', {'textFormat': {'bold': True}})
            sheet.format(f"A2:F{len(rows)}", {"wrapStrategy": "WRAP"})
            
            print(f"Successfully pushed calendar format to Google Sheet!")
        except Exception as e:
            print(f"Failed to push to Google Sheets: {e}")
    elif skip_sheets:
        print("Skipping Google Sheets push due to --skip-sheets flag.")

    # --- 2. Discord Alert Logic (Grouped and succinct) ---
    cheap_flights = [f for f in all_results if f['price'] <= threshold]
    
    if not DISCORD_WEBHOOK_URL:
        print("No discord webhook URL configured. Printing cheap flights to console instead.")
        if cheap_flights:
            print(f"\n=== 🚨 Flights Under ${threshold} ===")
            for f in sorted(cheap_flights, key=lambda x: x['price']):
                print(f"{f['origin']}->{f['destination']} | {f['dep_date']} to {f['ret_date']} | ${f['price']:.2f}")
        return
        
    if not cheap_flights:
        print(f"No flights under ${threshold} found. Skipping Discord alert.")
        return
        
    # Group by route and price to reduce spam
    # e.g. (SFO, LAX, 90.82) -> ["2026-04-10 to 2026-04-13", "2026-04-17 to 2026-04-20"]
    grouped_flights = defaultdict(list)
    for f in cheap_flights:
        key = (f['origin'], f['destination'], f['price'], f['type'])
        grouped_flights[key].append(f"{f['dep_date']} to {f['ret_date']}")
        
    message_lines = [f"# 🚨 CHEAP FLIGHT ALERT: Found {len(cheap_flights)} flights under ${threshold}!"]
    
    # Sort by price ascending
    sorted_groups = sorted(grouped_flights.items(), key=lambda x: x[0][2])
    
    for (origin, dest, price, btype), date_ranges in sorted_groups:
        dates_str = ", ".join(date_ranges)
        # If there are too many dates, truncate
        if len(dates_str) > 100:
            dates_str = dates_str[:97] + "..."
        message_lines.append(f"- `{origin} ✈️ {dest}` | **${price:.2f}** ({btype}) | {dates_str}")
        
    full_msg = "\n".join(message_lines)
    chunks = [full_msg[i:i+1900] for i in range(0, len(full_msg), 1900)]
    
    for chunk in chunks:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk})
            time.sleep(1.5)
        except Exception as e:
            print(f"Failed to send Discord alert: {e}")


def main():
    parser = argparse.ArgumentParser(description="Flight Tracker")
    parser.add_argument('--days', type=int, default=180, help='Number of days ahead to search (default 180)')
    parser.add_argument('--skip-sheets', action='store_true', help='Skip pushing to Google Sheets')
    parser.add_argument('--threshold', type=float, default=DISCORD_PRICE_THRESHOLD, help='Discord price alert threshold')
    args = parser.parse_args()

    start_date = datetime.now()
    dates = get_flight_dates(start_date, args.days)
    
    all_results = []
    routes = []
    for bay in BAY_AREA_AIRPORTS:
        for la in LA_AIRPORTS:
            routes.append((bay, la))
            routes.append((la, bay))
            
    print(f"Starting search for {len(dates)} weekends across {len(routes)} routes (Next {args.days} days)...")
        
    for index, (origin, dest) in enumerate(routes):
        for dep_date, ret_date in dates:
            # We ONLY check Round Trips to drastically conserve the Amadeus API quota
            rt_price = fetch_cheapest_flight(origin, dest, dep_date, ret_date)
            time.sleep(0.3)
            
            best_price = rt_price
            booking_type = "RoundTrip" if rt_price is not None else None
            
            if best_price is not None:
                is_anniv = ('-03-21' in dep_date or '-03-21' in ret_date or ('-03-20' in dep_date and '-03-22' in ret_date))
                all_results.append({
                    'origin': origin,
                    'destination': dest,
                    'dep_date': dep_date,
                    'ret_date': ret_date,
                    'price': best_price,
                    'type': booking_type,
                    'is_anniv': is_anniv
                })
                
            print(f"[{origin}->{dest}] {dep_date} to {ret_date} | Best: ${best_price}")
                
    process_results(all_results, skip_sheets=args.skip_sheets, threshold=args.threshold)
    print("Done!")

if __name__ == "__main__":
    main()
