"""
An agent uses the todo app to plan and execute a project.

The agent is given the todo API help page, then asked to:
  1. Create a task breakdown for a project
  2. Work through them (marking done, adding subtasks, setting priorities)

Usage:
    python example/todo_agent.py
"""

import json
import re
import subprocess
import tempfile
import threading
import time

import requests

from inact import Inact, mount_todo

BASE_URL = "http://127.0.0.1:17433"


def start_server() -> str:
    db = tempfile.mktemp(suffix=".db")
    app = Inact("todo-agent")
    mount_todo(app, "/tasks", db)
    threading.Thread(
        target=lambda: app.run(port=17433, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.8)
    return db


def claude(prompt: str) -> str:
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def get(path: str) -> str:
    return requests.get(BASE_URL + path).text


def post(path: str, body: dict) -> str:
    return requests.post(BASE_URL + path, json=body).text


def task_id(response: str) -> str:
    m = re.search(r'id\s+=\s+"([^"]+)"', response)
    return m.group(1) if m else ""


def print_task_list():
    raw = get("/tasks/")
    print("\n--- current task list ---")
    for block in raw.split("[[tasks]]")[1:]:
        title = re.search(r'title\s+=\s+"([^"]+)"', block)
        status = re.search(r'status\s+=\s+"([^"]+)"', block)
        priority = re.search(r'priority\s+=\s+"([^"]+)"', block)
        assignee = re.search(r'assignee\s+=\s+"([^"]+)"', block)
        due = re.search(r'due\s+=\s+"([^"]+)"', block)
        parts = [f"  [{status.group(1) if status else '?'}]"]
        parts.append(f"({priority.group(1) if priority else '?'})")
        parts.append(title.group(1) if title else "?")
        if assignee and assignee.group(1):
            parts.append(f"@{assignee.group(1)}")
        if due:
            parts.append(f"due:{due.group(1)}")
        print(" ".join(parts))
    print()


def main():
    print("Starting todo server...")
    db = start_server()
    help_page = get("/tasks/.help") if False else ""  # not needed, describe API inline

    api_docs = """You have access to a todo REST API at http://127.0.0.1:17433/tasks/:

POST /tasks/             create task  body: {"title":"...","description":"...","priority":"low|normal|high|urgent","due":"YYYY-MM-DD","assignee":"..."}
GET  /tasks/             list tasks
GET  /tasks/{id}         task detail
POST /tasks/{id}/.done   mark done
POST /tasks/{id}/.assign body: {"assignee":"..."}
DELETE /tasks/{id}       delete

Respond ONLY with a JSON array of objects. Each object must have:
  "action": one of "create" | "done" | "assign"
  For "create": "title", and optionally "description", "priority", "due", "assignee"
  For "done"/"assign": "title" (to match the task to operate on)
  For "assign": also "assignee"

Do not include any explanation or markdown, just the raw JSON array."""

    # ------------------------------------------------------------------ step 1
    print("\n[step 1] asking claude to plan a project...\n")
    plan_prompt = (
        api_docs + "\n\n"
        "You are a software engineering agent. "
        "Plan the tasks needed to build a basic REST API service with authentication. "
        "Create 8-10 tasks with appropriate priorities, due dates (use 2026-05-XX dates), "
        "and assign each to one of: alice, bob, carol."
    )
    plan_json = claude(plan_prompt)
    print("claude says:", plan_json[:300], "...\n")

    try:
        actions = json.loads(plan_json)
    except json.JSONDecodeError:
        # try to extract JSON array from the response
        m = re.search(r'\[.*\]', plan_json, re.DOTALL)
        actions = json.loads(m.group(0)) if m else []

    created: dict[str, str] = {}  # title -> id
    for a in actions:
        if a.get("action") == "create":
            body = {k: v for k, v in a.items() if k != "action"}
            resp = post("/tasks/", body)
            tid = task_id(resp)
            if tid:
                created[a["title"]] = tid
                print(f"  created: [{a.get('priority','normal')}] {a['title']}")

    print_task_list()

    # ------------------------------------------------------------------ step 2
    print("[step 2] asking claude to do some work (mark things done, add more)...\n")
    current_list = get("/tasks/")
    # summarise for claude
    task_lines = []
    for block in current_list.split("[[tasks]]")[1:]:
        t = re.search(r'title\s+=\s+"([^"]+)"', block)
        s = re.search(r'status\s+=\s+"([^"]+)"', block)
        i = re.search(r'id\s+=\s+"([^"]+)"', block)
        if t and s and i:
            task_lines.append(f"  id={i.group(1)} status={s.group(1)} title={t.group(1)!r}")

    work_prompt = (
        api_docs + "\n\n"
        "You are a software engineering agent reviewing your project progress. "
        "Current tasks:\n" + "\n".join(task_lines) + "\n\n"
        "Mark the setup and design tasks as done. "
        "Also add 2 new urgent tasks that you just discovered are needed. "
        "Use the 'done' action with the exact task title to match."
    )
    work_json = claude(work_prompt)
    print("claude says:", work_json[:300], "...\n")

    try:
        work_actions = json.loads(work_json)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', work_json, re.DOTALL)
        work_actions = json.loads(m.group(0)) if m else []

    for a in work_actions:
        if a.get("action") == "done":
            title = a.get("title", "")
            tid = created.get(title)
            if not tid:
                # fuzzy match
                for t, i in created.items():
                    if title.lower() in t.lower() or t.lower() in title.lower():
                        tid = i
                        break
            if tid:
                post(f"/tasks/{tid}/.done", {})
                print(f"  marked done: {title}")
            else:
                print(f"  (no match for: {title!r})")
        elif a.get("action") == "create":
            body = {k: v for k, v in a.items() if k != "action"}
            resp = post("/tasks/", body)
            tid = task_id(resp)
            if tid:
                created[a["title"]] = tid
                print(f"  created: [{a.get('priority','normal')}] {a['title']}")

    print_task_list()

    # ------------------------------------------------------------------ step 3
    print("[step 3] final summary from claude...\n")
    final_list = get("/tasks/")
    done_count = final_list.count('status = "done"')
    todo_count = final_list.count('status = "todo"')

    summary_prompt = (
        "You are a project manager. Here is the current state of a software project's tasks:\n\n"
        + final_list[:2000]
        + f"\n\nSummary stats: {done_count} done, {todo_count} remaining. "
        "Write a brief (3-4 sentence) project status update."
    )
    summary = claude(summary_prompt)
    print("Project status:\n" + summary)

    import os; os.unlink(db)


if __name__ == "__main__":
    main()
