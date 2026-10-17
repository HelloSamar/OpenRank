--- a/quiz_bot.py
+++ b/quiz_bot.py
@@
 import argparse
 import json
 import logging
 import os
 import re
 import sys
 import tempfile
 import time
 from dataclasses import dataclass
 from datetime import datetime, timezone
 from pathlib import Path
 from typing import Any, Dict, List, Tuple
@@
 TELEGRAM_API_BASE = "https://api.telegram.org"
 
-DELAY_BETWEEN_POLLS = 15        # seconds to wait between polls
+DELAY_BETWEEN_POLLS = 15        # seconds to wait between polls
 MAX_ATTEMPTS = 3                # total tries per poll (1 initial + 2 retries)
 BACKOFF_BASE = 2                # retry delay = BACKOFF_BASE ** attempt seconds
 MAX_RETRY_AFTER = 120           # cap for Telegram's "retry_after" (HTTP 429)
 REQUEST_TIMEOUT = (10, 30)      # (connect, read) timeouts in seconds
+POLL_OPEN_PERIOD = 30           # Telegram poll lifetime in seconds (5-600)
@@
 logger = logging.getLogger("quiz_bot")
+
+
+def send_chat_message(
+    session: requests.Session,
+    token: str,
+    chat_id: str,
+    text: str,
+) -> None:
+    """Send a normal chat message."""
+    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
+    payload = {
+        "chat_id": chat_id,
+        "text": text,
+        "parse_mode": "HTML",
+    }
+    try:
+        session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
+    except requests.exceptions.RequestException as exc:
+        logger.warning("Could not send announcement message: %s", redact(exc, token))
+
+
+def listen_for_poll_answers(
+    session: requests.Session,
+    token: str,
+    chat_id: str,
+    poll_id: str,
+    option_texts: List[str],
+    window_secs: int = 30,
+) -> None:
+    """Poll Telegram for PollAnswer updates and announce each response.
+
+    Telegram needs non-anonymous polls for this to work.
+    This uses getUpdates with allowed_updates=["poll_answer"].
+    """
+    deadline = time.time() + max(5, window_secs)
+    offset = None
+
+    while time.time() < deadline:
+        params = {
+            "timeout": 2,
+            "allowed_updates": json.dumps(["poll_answer"]),
+        }
+        if offset is not None:
+            params["offset"] = offset
+
+        try:
+            response = session.get(
+                f"{TELEGRAM_API_BASE}/bot{token}/getUpdates",
+                params=params,
+                timeout=REQUEST_TIMEOUT,
+            )
+            data = response.json() if response.ok else {}
+        except requests.exceptions.RequestException as exc:
+            logger.warning("getUpdates error while listening for answers: %s",
+                           redact(exc, token))
+            time.sleep(1)
+            continue
+
+        if not isinstance(data, dict):
+            time.sleep(1)
+            continue
+
+        for update in data.get("result", []) or []:
+            update_id = update.get("update_id")
+            if update_id is not None:
+                offset = update_id + 1
+
+            poll_answer = update.get("poll_answer")
+            if not poll_answer:
+                continue
+            if poll_answer.get("poll_id") != poll_id:
+                continue
+
+            user = poll_answer.get("user") or {}
+            username = user.get("username")
+            if not username:
+                first = user.get("first_name") or ""
+                last = user.get("last_name") or ""
+                username = " ".join(part for part in (first, last) if part).strip()
+            if not username:
+                username = f"user:{user.get('id', 'unknown')}"
+
+            option_ids = poll_answer.get("option_ids") or []
+            selected_text = "(no option)"
+            if option_ids:
+                option_index = option_ids[0]
+                if 0 <= option_index < len(option_texts):
+                    selected_text = option_texts[option_index]
+                else:
+                    selected_text = f"option #{option_index}"
+
+            text = f"✅ <b>{username}</b> answered: {selected_text}"
+            send_chat_message(session, token, chat_id, text)
+
+        time.sleep(0.25)
@@
 def send_quiz_poll(
     session: requests.Session,
     token: str,
     chat_id: str,
@@
     payload = {
         "chat_id": chat_id,
         "question": question,
         "options": list(options),
         "type": "quiz",
-        "is_anonymous": True,
+        "is_anonymous": False,
         "correct_option_id": correct_index,
-        "open_period": 15,
+        "open_period": POLL_OPEN_PERIOD,
     }
@@
             if response.status_code == 200 and data.get("ok") is True:
+                result = data.get("result") or {}
+                poll = result.get("poll") or {}
+                poll_id = poll.get("id")
+                option_texts = [opt.get("text", "") for opt in poll.get("options", [])]
+                if poll_id:
+                    logger.info("Poll %s created; listening for answers for %d seconds.",
+                                poll_id, POLL_OPEN_PERIOD)
+                    listen_for_poll_answers(
+                        session,
+                        token,
+                        chat_id,
+                        poll_id,
+                        option_texts,
+                        window_secs=POLL_OPEN_PERIOD,
+                    )
                 return True
