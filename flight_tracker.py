import os
import time
import json
import requests
import gspread
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

def fetch_cheapest_flight(origin, destination, dep_date, ret_date=None):
    if not access_token:
        return None
        
    try:
        headers = {'Authorization': f'Bearer {access_token}'}
        params = {
            'originLocationCode': origin,
            'destinationLocationCode': destination,
            'departureDate': dep_date,
            'adults': 1,
            'currencyCode': 'USD',
            'max': 1 # Get top 1 because we only care about the cheapest
        }
        if ret_date:
            params['returnDate'] = ret_date
            
        res = requests.get(AMADEUS_SEARCH_URL, headers=headers, params=params)
        res.raise_for_status()
        data = res.json().get('data', [])
        if data:
            return float(data[0]['price']['total'])
    except Exception as error:
        # Ignore individual errors due to high volume, handle silently
        pass
    return None

def format_flight_entry(f):
    anniv_flag = " 🎉 **ANNIV!**" if f.get('is_anniv') else ""
    return f"`{f['origin']} ✈️ {f['destination']}` | {f['dep_date']} to {f['ret_date']} | **${f['price']:.2f}** ({f['type']}){anniv_flag}"

def push_to_sheets_and_discord(all_results):
    # 1. Google Sheets Logic
    if gc and GOOGLE_SHEET_ID:
        try:
            sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
            
            # Prepare rows
            rows = [["Origin", "Destination", "Departure Date", "Return Date", "Price", "Booking Type", "Is Anniversary"]]
            # Sort all flights by date then price for the sheet
            sorted_results = sorted(all_results, key=lambda x: (x['dep_date'], x['price']))
            for f in sorted_results:
                rows.append([
                    f['origin'], 
                    f['destination'], 
                    f['dep_date'], 
                    f['ret_date'], 
                    f['price'], 
                    f['type'], 
                    "Yes" if f.get('is_anniv') else "No"
                ])
                
            sheet.clear()
            sheet.update(f"A1:G{len(rows)}", rows)
            
            # Format header row to be bold
            sheet.format('A1:G1', {'textFormat': {'bold': True}})
            print(f"Successfully pushed {len(all_results)} flights to Google Sheet!")
        except Exception as e:
            print(f"Failed to push to Google Sheets: {e}")
    else:
        print("Skipping Google Sheets push (credentials or Sheet ID missing).")
        
    # 2. Discord Alert Logic (ONLY sub-$100 flights)
    cheap_flights = [f for f in all_results if f['price'] < 100.0]
    
    if not DISCORD_WEBHOOK_URL:
        print("No discord webhook URL configured. Printing cheap flights to console instead.")
        if cheap_flights:
            print(f"\n=== 🚨 {len(cheap_flights)} Flights Under $100 ===")
            for f in sorted(cheap_flights, key=lambda x: x['price']):
                print(format_flight_entry(f))
        return
        
    if not cheap_flights:
        print("No flights under $100 found. Skipping Discord alert.")
        return
        
    # Sort and format for Discord
    cheap_flights.sort(key=lambda x: x['price'])
    
    message_lines = [f"# 🚨 CHEAP FLIGHT ALERT: {len(cheap_flights)} Flights Under $100!"]
    for f in cheap_flights:
        message_lines.append(f"- {format_flight_entry(f)}")
        
    full_msg = "\n".join(message_lines)
    chunks = [full_msg[i:i+1900] for i in range(0, len(full_msg), 1900)]
    
    for chunk in chunks:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk})
            time.sleep(1.5)
        except Exception as e:
            print(f"Failed to send Discord alert: {e}")

def main():
    start_date = datetime.now()
    dates = get_flight_dates(start_date, 180)
    
    all_results = []
    
    # Map of routes to consider:
    routes = []
    for bay in BAY_AREA_AIRPORTS:
        for la in LA_AIRPORTS:
            routes.append((bay, la))
            routes.append((la, bay))
            
    print(f"Starting flight search for {len(dates)} weekends across {len(routes)} routes...")
    print("Evaluating Round-Trips vs 2x One-Ways globally.")
    
    # Cache one-ways so we don't query same day multiple times
    one_way_cache = {}
    def get_one_way(origin, dest, date):
        key = (origin, dest, date)
        if key not in one_way_cache:
            one_way_cache[key] = fetch_cheapest_flight(origin, dest, date)
            time.sleep(0.3)
        return one_way_cache[key]
        
    for index, (origin, dest) in enumerate(routes):
        for dep_date, ret_date in dates:
            # 1. Price as Round Trip
            rt_price = fetch_cheapest_flight(origin, dest, dep_date, ret_date)
            time.sleep(0.3)
            
            # 2. Price as Two One-Ways
            ow_out = get_one_way(origin, dest, dep_date)
            ow_in = get_one_way(dest, origin, ret_date)
            
            best_price = None
            booking_type = None
            
            prices = []
            if rt_price is not None:
                prices.append((rt_price, "RoundTrip"))
            if ow_out is not None and ow_in is not None:
                prices.append((ow_out + ow_in, "2x OneWay"))
                
            if prices:
                prices.sort(key=lambda x: x[0])
                best_price, booking_type = prices[0]
                
            if best_price is not None:
                # March 21st anniversary check
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
                
            print(f"[{origin}->{dest}] {dep_date} to {ret_date} | Best: ${best_price} ({booking_type})")
                
    push_to_sheets_and_discord(all_results)
    print("Done!")

if __name__ == "__main__":
    main()
