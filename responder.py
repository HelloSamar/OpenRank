"""Small command responder for the OpenRank group bot.

quiz_bot.py calls this on every scheduled run (about every 15 minutes), so replies are
batched and can take up to ~15 minutes (longer while a quiz is being posted).

Commands (the same list you give to BotFather, see COMMANDS):
    /start      asks the person to join the group (hyperlinked)
    /quizlists  every stored quiz: its name and number of questions
    /contact    ask for a quiz on a topic or subject, or write to the owner: opens a direct
                chat with the owner (button) and also passes a written message on
(/request still works as another name for /contact.)

The owner can answer a forwarded message simply by replying to it in the bot's chat: the
reply is passed on to the person. Only the owner's replies are relayed.

This module needs only the standard library. It talks to Telegram through the
`api(method, payload)` function it is given and never imports quiz_bot.
"""

import html
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("quiz_bot")

COMMANDS: List[Tuple[str, str]] = [
    ("start", "Join our group"),
    ("quizlists", "See all quizzes and how many questions each has"),
    ("contact", "Request a quiz or chat with the owner directly"),
]

GROUP_URL = "https://t.me/OpenScore"      # the group /start sends people to

MAX_TEXT_LENGTH = 1000          # longest message that is forwarded
MAX_PER_USER = 5                # commands answered per person in one run (spam guard)
MAX_RELAY_LENGTH = 3500         # longest owner reply that is passed on

START_TEXT = ("\U0001F44B Hi {name}!\n\n"
              'Please <a href="{url}">join our group</a> to take part in the daily quizzes.')

CONTACT_MARKER = "\u2709\uFE0F Message to the owner"     # first line of the reply prompt
MENU_MARKER = "\u2709\uFE0F Contact the owner"           # first line of the contact menu
OLD_REQUEST_MARKER = "\U0001F4DD Quiz request"            # prompts sent by an earlier version
PROMPT_HEADINGS = {CONTACT_MARKER, MENU_MARKER, OLD_REQUEST_MARKER}

CONTACT_PROMPT = (
    f"{CONTACT_MARKER}\n\n"
    "Reply to this message with what you would like to tell the owner. Want a quiz on a "
    "specific exam topic or subject? Write that here too.\n"
    "Example: SSC CGL \u2013 Reasoning \u2013 syllogisms")
CONTACT_PLACEHOLDER = "Your message to the owner"
CONTACT_MENU_TEXT = (
    f"{MENU_MARKER}\n\n"
    "Want a quiz on a specific exam topic or subject, or have a question?\n\n"
    "Tap the button below to chat with the owner directly.\n"
    "The owner will respond soon! Thanks for reaching out. Please share with friends so we can grow together!")
CONTACT_DIRECT_TEXT = (
    f"{MENU_MARKER}\n\n"
    "Want a quiz on a specific exam topic or subject, or have a question? "
    "Tap the button below to chat with the owner directly.")
CONTACT_BUTTON = "\U0001F4AC Chat with the owner"
NOTE_HEADING = "\u2709\uFE0F Message"
SENT_TEXT = "\u2705 Your message was sent to the owner. If they reply, you will get it here."
UNAVAILABLE_TEXT = "Sorry, this is not available right now. Please try again later."
HINT_TEXT = "I only understand commands. Try /start or /quizlists or /contact."

TAG = re.compile(r"#u(\d+)")

ApiFunc = Callable[[str, Dict[str, Any]], Any]


def parse_command(text: str) -> Tuple[str, str]:
    """'/request@MyBot SSC CGL' -> ('request', 'SSC CGL'); ordinary text -> ('', text)."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    return head[1:].split("@", 1)[0].lower(), rest.strip()


def wanted(update: Dict[str, Any]) -> bool:
    """True for message updates worth keeping: private chats, commands and replies.

    Used to keep ordinary group chatter (if the bot can see it) out of memory.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return False
    return (chat.get("type") == "private" or text.lstrip().startswith("/")
            or isinstance(message.get("reply_to_message"), dict))


def display_name(user: Dict[str, Any]) -> str:
    name = " ".join(str(user[k]) for k in ("first_name", "last_name") if user.get(k))
    return name or str(user.get("username") or user.get("id"))


class Responder:
    def __init__(self, api: ApiFunc, quiz_list_text: Callable[[], str],
                 owner_id: Optional[int] = None, max_per_user: int = MAX_PER_USER,
                 owner_username: Optional[str] = None):
        self.api = api
        self.quiz_list_text = quiz_list_text
        self.owner_id = owner_id
        self.owner_username = owner_username
        self.max_per_user = max_per_user
        self.counts: Dict[int, int] = {}
        self._redirected: Set[int] = set()
        self._username: Optional[str] = None

    # ----- sending -------------------------------------------------------- #
    def say(self, chat_id: int, text: str, force_reply: Optional[str] = None,
            html_text: bool = False, button: Optional[Tuple[str, str]] = None) -> bool:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if html_text:
            payload["parse_mode"] = "HTML"
            payload["link_preview_options"] = {"is_disabled": True}
        if force_reply:
            payload["reply_markup"] = {"force_reply": True,
                                       "input_field_placeholder": force_reply}
        elif button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0],
                                                              "url": button[1]}]]}
        result = self.api("sendMessage", payload)
        if not result.ok:
            logger.warning("Could not message chat %s: %s", chat_id,
                           getattr(result, "description", ""))
        return bool(result.ok)

    def username(self) -> Optional[str]:
        if self._username is None:
            result = self.api("getMe", {})
            info = result.data.get("result") if result.ok else None
            self._username = (info.get("username") or "") if isinstance(info, dict) else ""
        return self._username or None

    # ----- main entry ----------------------------------------------------- #
    def handle(self, update: Dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat, user, text = message.get("chat"), message.get("from"), message.get("text")
        if (not isinstance(chat, dict) or not isinstance(chat.get("id"), int)
                or not isinstance(user, dict) or not isinstance(user.get("id"), int)
                or user.get("is_bot") or not isinstance(text, str)):
            return
        private = chat.get("type") == "private"

        if private and self.owner_id is not None and user["id"] == self.owner_id \
                and self._relay_owner_reply(message):
            return

        command, rest = parse_command(text)
        if command == "request":
            command = "contact"                      # the old name still works
        is_reply = self._is_prompt_reply(message)
        if not command and not is_reply:
            if private:
                self._counted(user, lambda: self.say(chat["id"], HINT_TEXT))
            return                                    # ordinary chatter elsewhere: ignore

        if not private:
            if command in dict(COMMANDS):
                self._redirect(chat["id"])
            return

        if command == "start":
            name = html.escape(str(user.get("first_name") or "there"))
            self._counted(user, lambda: self.say(
                chat["id"], START_TEXT.format(name=name, url=GROUP_URL), html_text=True))
        elif command == "quizlists":
            self._counted(user, lambda: self.say(chat["id"], self.quiz_list_text()))
        elif command == "contact":
            self._counted(user, lambda: self._contact(user, chat["id"], rest))
        elif command:
            self._counted(user, lambda: self.say(chat["id"], HINT_TEXT))
        else:                                         # a reply to one of our messages
            self._counted(user, lambda: self._contact(user, chat["id"], text))

    # ----- helpers -------------------------------------------------------- #
    def _counted(self, user: Dict[str, Any], action: Callable[[], Any]) -> None:
        count = self.counts.get(user["id"], 0) + 1
        self.counts[user["id"]] = count
        if count > self.max_per_user:
            if count == self.max_per_user + 1:
                logger.warning("Ignoring further commands from user %s in this run.",
                               user["id"])
            return
        action()

    @staticmethod
    def _is_prompt_reply(message: Dict[str, Any]) -> bool:
        """True when this message answers one of our contact prompts."""
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict):
            return False
        sender = reply.get("from")
        original = reply.get("text")
        if not isinstance(sender, dict) or not sender.get("is_bot") \
                or not isinstance(original, str):
            return False
        # Only the exact heading counts: a forwarded note ("... #u42") is not a prompt.
        return original.split("\n", 1)[0] in PROMPT_HEADINGS

    def _redirect(self, chat_id: int) -> None:
        if chat_id in self._redirected:
            return
        self._redirected.add(chat_id)
        name = self.username()
        where = f"https://t.me/{name}" if name else "a private chat with me"
        self.say(chat_id, f"Please send this command to me in a private chat: {where}")

    def _direct_chat_link(self) -> Optional[str]:
        return f"https://t.me/{self.owner_username}" if self.owner_username else None

    def _contact(self, user: Dict[str, Any], chat_id: int, text: str) -> None:
        """/contact: text is passed on to the owner; no text offers the ways to reach them."""
        text = text.strip()
        link = self._direct_chat_link()
        button = (CONTACT_BUTTON, link) if link else None
        if not text:
            if self.owner_id is not None and button:
                self.say(chat_id, CONTACT_MENU_TEXT, button=button)
            elif self.owner_id is not None:
                self.say(chat_id, CONTACT_PROMPT, force_reply=CONTACT_PLACEHOLDER)
            elif button:
                self.say(chat_id, CONTACT_DIRECT_TEXT, button=button)
            else:
                self.say(chat_id, UNAVAILABLE_TEXT)
            return
        if len(text) > MAX_TEXT_LENGTH:
            self.say(chat_id, f"That is too long. Please keep it under {MAX_TEXT_LENGTH} "
                              "characters and send it again.")
            return
        if self.owner_id is None:
            logger.warning("A message from user %s (%s) could not be forwarded: OWNER_CHAT_ID "
                           "is not set.", user["id"], display_name(user))
            if button:
                self.say(chat_id, CONTACT_DIRECT_TEXT, button=button)
            else:
                self.say(chat_id, UNAVAILABLE_TEXT)
            return
        handle = f" (@{user['username']})" if user.get("username") else ""
        note = (f"{NOTE_HEADING} #u{user['id']}\n"
                f"From: {display_name(user)}{handle} \u00B7 id {user['id']}\n\n{text}\n\n"
                "\u21A9\uFE0F Reply to this message to answer.")
        if self.say(self.owner_id, note):
            self.say(chat_id, SENT_TEXT)
        else:
            logger.error("Could not deliver a message to the owner. The owner must open the bot "
                         "and press START once, and OWNER_CHAT_ID must be their user id.")
            if button:
                self.say(chat_id, CONTACT_DIRECT_TEXT, button=button)
            else:
                self.say(chat_id, UNAVAILABLE_TEXT)

    def _relay_owner_reply(self, message: Dict[str, Any]) -> bool:
        """Pass the owner's reply to a forwarded note on to the person. True if it was one."""
        reply = message.get("reply_to_message")
        text = message.get("text")
        if not isinstance(reply, dict) or not isinstance(text, str):
            return False
        sender = reply.get("from")
        original = reply.get("text")
        if not isinstance(sender, dict) or not sender.get("is_bot") \
                or not isinstance(original, str):
            return False
        match = TAG.search(original.split("\n", 1)[0])
        if match is None:
            return False
        delivered = self.say(int(match.group(1)),
                             "\U0001F4AC Reply from the owner:\n\n" + text[:MAX_RELAY_LENGTH])
        self.say(self.owner_id or 0, "\u2705 Sent." if delivered else
                 "\u26A0\uFE0F Could not deliver (the person may have blocked the bot).")
        return True
