"""
An agent reads and edits a CSV file via the files app.

Usage:
    python example/csv_agent.py
"""

import json
import re
import subprocess
import tempfile
import threading
import time
import os

import requests

from inact import Inact, mount_files, CSVHandler

BASE_URL = "http://127.0.0.1:17441"


def start_server(folder: str) -> None:
    app = Inact("csv-agent")
    mount_files(app, "/data", folder, handlers=[CSVHandler(rows_per_page=20)])
    threading.Thread(
        target=lambda: app.run(port=17441, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.8)


def claude(prompt: str) -> str:
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def get(path: str) -> str:
    return requests.get(BASE_URL + path).text


def append_row(file_path: str, row: dict | list) -> str:
    return requests.post(BASE_URL + file_path + "/.append", json=row).text


def print_csv(path: str):
    print(get(f"/data/{path}/p/1"))


def main():
    tmp = tempfile.mkdtemp()
    csv_file = os.path.join(tmp, "sales.csv")

    # Seed the CSV with some initial data
    with open(csv_file, "w") as f:
        f.write("date,product,quantity,unit_price,salesperson,region\n")
        f.write("2026-04-01,Widget A,5,29.99,Alice,North\n")
        f.write("2026-04-02,Widget B,3,49.99,Bob,South\n")
        f.write("2026-04-03,Widget A,8,29.99,Carol,East\n")
        f.write("2026-04-05,Gadget X,2,149.99,Alice,North\n")
        f.write("2026-04-06,Widget B,10,49.99,Dave,West\n")

    print("Starting server...")
    start_server(tmp)

    # ------------------------------------------------------------------ step 1
    print("\n[step 1] show agent the current data and ask it to analyse\n")
    current = get("/data/sales.csv/p/1")
    print("Current CSV:")
    print(current)

    analysis_prompt = (
        "You are a data analyst. Here is a sales CSV:\n\n"
        + current
        + "\n\nBriefly analyse the data (2-3 sentences): top product, top salesperson, "
        "any patterns you notice."
    )
    analysis = claude(analysis_prompt)
    print("Agent analysis:")
    print(analysis)

    # ------------------------------------------------------------------ step 2
    print("\n[step 2] ask agent to generate new sales rows to append\n")
    append_prompt = (
        "You are a sales data entry agent. The sales CSV has columns: "
        "date, product, quantity, unit_price, salesperson, region\n\n"
        "Existing data:\n" + current + "\n\n"
        "Generate 6 new realistic sales rows for late April 2026. "
        "Use the same products (Widget A at 29.99, Widget B at 49.99, Gadget X at 149.99) "
        "and salespeople (Alice, Bob, Carol, Dave). Mix regions (North/South/East/West).\n\n"
        "Respond ONLY with a JSON array of arrays, each inner array being "
        "[date, product, quantity, unit_price, salesperson, region]. "
        "No explanation, no markdown, just the raw JSON array."
    )
    rows_json = claude(append_prompt)
    print("Agent generated rows:", rows_json[:200], "...\n")

    try:
        new_rows = json.loads(rows_json)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', rows_json, re.DOTALL)
        new_rows = json.loads(m.group(0)) if m else []

    for row in new_rows:
        r = append_row("/data/sales.csv", row)
        print(f"  appended: {row}  →  {r.strip()}")

    # ------------------------------------------------------------------ step 3
    print("\n[step 3] show updated file and ask for summary\n")
    info = get("/data/sales.csv/.info")
    updated = get("/data/sales.csv/p/1")
    print("Updated CSV (page 1):")
    print(updated)

    summary_prompt = (
        "You are a sales manager. Here is the updated sales log after new entries:\n\n"
        + updated
        + "\n\nFile info:\n" + info
        + "\n\nWrite a short end-of-month summary (3-4 sentences): "
        "total rows, who sold the most, which product is leading, "
        "and one recommendation for next month."
    )
    summary = claude(summary_prompt)
    print("Agent summary:")
    print(summary)

    import shutil
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
