"""File-based bridge between the running program and an external vision reader.

For each screenshot the program:
  1. saves the PNG (via capture.py),
  2. writes a `<png>.request.json` describing what to read,
  3. waits for a `<png>.result.json` to appear (the reader reads the PNG and writes it),
  4. or, if --manual-vision / timeout, falls back to human keyboard entry.

The scripts capture the screen and an external reader returns the values, so the
human never has to paste screenshots anywhere.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional


def write_request(image_path: str, kind: str, schema_hint: str) -> str:
    req_path = image_path + ".request.json"
    with open(req_path, "w", encoding="utf-8") as f:
        json.dump({
            "kind": kind,
            "image": os.path.abspath(image_path),
            "schema_hint": schema_hint,
            "result_path": os.path.abspath(image_path + ".result.json"),
        }, f, indent=2)
    return req_path


def wait_for_result(image_path: str, timeout_s: float = 180.0,
                    poll_s: float = 1.0) -> Optional[dict]:
    res_path = image_path + ".result.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data
            except (json.JSONDecodeError, OSError):
                time.sleep(poll_s)
                continue
        time.sleep(poll_s)
    return None


def request(image_path: str, kind: str, schema_hint: str,
            manual_fn: Callable[[], dict], *, manual: bool = False,
            timeout_s: float = 180.0, announce: Optional[Callable[[str], None]] = None) -> dict:
    """Get a structured reading for `image_path`.

    If `manual`, go straight to keyboard entry. Otherwise write the request,
    tell the operator to have the vision reader read it, and poll for the result
    file - falling back to manual entry on timeout.
    """
    say = announce or print
    if manual:
        return manual_fn()

    write_request(image_path, kind, schema_hint)
    say(f"[vision] Screenshot saved: {image_path}")
    say(f"[vision] Vision reader: read this image and write {os.path.basename(image_path)}.result.json")
    say(f"         (schema: {schema_hint})")
    say("         Waiting for the reading... (Ctrl-C to enter values manually)")
    try:
        data = wait_for_result(image_path, timeout_s=timeout_s)
    except KeyboardInterrupt:
        data = None
    if data is None:
        say("[vision] No reading received - falling back to manual entry.")
        return manual_fn()
    return data
