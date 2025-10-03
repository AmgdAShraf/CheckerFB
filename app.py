import os
import re
import json
import requests
import time
import uuid
import threading

from flask import Flask, request, render_template, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# --- Headers (User-Agent, Accept-Language, etc.) ---
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
}

BASE_URL = "https://www.facebook.com/profile.php?id="

# --- Global dictionary to store progress for each session ---
check_sessions = {} # { session_id: { 'total_ids': N, 'checked_count': N, ... } }

# --- Function to process a single ID ---
def process_single_id(item_data, session_id):
    user_id = item_data["id"]
    original_line = item_data["original_line"]
    
    if not user_id:
        return {"original_line": original_line, "id": None, "status": "ID not found"}

    profile_url = f"{BASE_URL}{user_id}"
    status_info = "Unknown error" 

    try:
        response = requests.get(profile_url, headers=HEADERS, timeout=10)
        response.raise_for_status() 

        title_match = re.search(r'<title>(.*?)</title>', response.text, re.IGNORECASE | re.DOTALL)
        
        if title_match:
            page_title = title_match.group(1).strip()
            
            if "Page Not Found" in page_title or "Content Not Found" in page_title or "Sorry, this content isn't available" in page_title:
                status_info = "Not Found"
            elif page_title.lower().startswith("facebook"):
                # This could be a generic Facebook page, potentially indicating a blocked/deleted account,
                # or a very old account without much public info.
                status_info = f"Blocked - {page_title}"
            else:
                # Assuming if it has a specific title and not "Page Not Found", it's an available profile.
                status_info = f"Available - {page_title}"
        else:
            status_info = "Unknown (No Title)"

    except requests.exceptions.Timeout:
        status_info = "Connection error: Request timed out"
    except requests.exceptions.HTTPError as e:
        status_info = f"HTTP error: {e}"
    except requests.exceptions.ConnectionError as e:
        status_info = f"Connection error: Could not connect ({e})"
    except requests.exceptions.RequestException as e:
        status_info = f"Request error: {e}"
    except Exception as e:
        status_info = f"Unexpected error: {e}"
            
    return {"original_line": original_line, "id": user_id, "status": status_info}

# --- Background task function to run the checking process ---
def run_check_in_background(session_id, ids_to_process, num_threads, include_account_name): # ADDED include_account_name
    # Retrieve the session_info once from the global dictionary
    session_info = check_sessions[session_id] 
    session_info['start_time'] = time.time()
    
    local_results = [] # Collect results in this thread before updating global
    
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        # Pass the item dictionary directly to the future
        future_to_item = {executor.submit(process_single_id, item, session_id): item for item in ids_to_process}
        
        for future in as_completed(future_to_item):
            result = future.result()
            local_results.append(result)

            with app.app_context():
                if result['status'].startswith("Available"):
                    check_sessions[session_id]['live_accounts'] += 1
                elif result['status'].startswith("Blocked"):
                    check_sessions[session_id]['blocked_accounts'] += 1
                else: # Covers "Not Found", "ID not found", "Unknown (No Title)", and all "error" statuses
                    check_sessions[session_id]['errors'] += 1
                check_sessions[session_id]['checked_count'] += 1
                
    # Once all futures are completed, store the final detailed results
    session_info['final_results'] = local_results
    session_info['end_time'] = time.time()
    session_info['status'] = 'completed'


# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_check', methods=['POST'])
def start_check():
    data = request.json
    ids_input = data.get('ids_input', '')
    num_threads = int(data.get('num_threads', 5))
    include_account_name = data.get('include_account_name', False) # ADDED this line
    
    if not ids_input:
        return jsonify({"error": "Please provide IDs to check."}), 400

    lines_data = []
    for line_num, line in enumerate(ids_input.splitlines()):
        original_line = line.strip()
        if not original_line: # Skip empty lines
            continue
        match = re.search(r'\b(\d{10,})\b', original_line)
        if match:
            extracted_id = match.group(1)
            lines_data.append({"original_line": original_line, "id": extracted_id})
        else:
            lines_data.append({"original_line": original_line, "id": None})
    
    if not lines_data:
        return jsonify({"error": "No valid IDs found in the input."}), 400

    session_id = str(uuid.uuid4()) # Generate a unique ID for this check session
    
    check_sessions[session_id] = {
        'total_ids': len(lines_data),
        'checked_count': 0,
        'live_accounts': 0,
        'blocked_accounts': 0,
        'errors': 0,
        'status': 'running',
        'start_time': None,
        'end_time': None,
        'final_results': [], # To store detailed results once completed
        'include_account_name': include_account_name # ADDED this line
    }
    
    # Start the checking process in a new thread
    thread = threading.Thread(target=run_check_in_background, args=(session_id, lines_data, num_threads, include_account_name)) # ADDED include_account_name
    thread.daemon = True 
    thread.start()

    return jsonify({"message": "Checking started successfully", "session_id": session_id})

@app.route('/get_progress/<session_id>', methods=['GET'])
def get_progress(session_id):
    session_info = check_sessions.get(session_id)
    if not session_info:
        return jsonify({"error": "Invalid session ID. Please restart the check."}), 404

    progress_data = {
        "total_ids": session_info['total_ids'],
        "checked_count": session_info['checked_count'],
        "live_accounts": session_info['live_accounts'],
        "blocked_accounts": session_info['blocked_accounts'],
        "errors": session_info['errors'],
        "status": session_info['status'],
        "elapsed_time": 0
    }

    if session_info['start_time']:
        if session_info['status'] == 'completed':
            progress_data['elapsed_time'] = round(session_info['end_time'] - session_info['start_time'], 2)
        else:
            progress_data['elapsed_time'] = round(time.time() - session_info['start_time'], 2)
    
    if session_info['status'] == 'completed':
        final_categorized_results = {
            "available": [],
            "blocked": [],
            "not_found": [],
            "successful_ids": []
        }
        include_account_name_in_output = session_info.get('include_account_name', False) # ADDED this line
        
        for item in session_info['final_results']:
            profile_status = item["status"]
            original_line = item["original_line"]
            user_id = item["id"]
            
            if profile_status.startswith("Available"): 
                title_part = profile_status.replace("Available - ", "")
                if include_account_name_in_output:
                    final_categorized_results["available"].append(f"{title_part}:{original_line}")
                else:
                    final_categorized_results["available"].append(f"{original_line}")
                if user_id: 
                    final_categorized_results["successful_ids"].append(user_id)
            elif profile_status.startswith("Blocked"):
                title_part = profile_status.replace("Blocked - ", "")
                if include_account_name_in_output:
                    final_categorized_results["blocked"].append(f"{title_part}: {original_line}")
                else:
                    final_categorized_results["blocked"].append(f"Blocked: {original_line}")
            else: # Catches "Not Found", "ID not found", "Unknown (No Title)", and all errors
                final_categorized_results["not_found"].append(f"{profile_status}: {original_line}")
        
        progress_data['final_results'] = final_categorized_results

    return jsonify(progress_data)

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
