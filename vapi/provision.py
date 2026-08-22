"""Create or update the Vapi assistant from version-controlled config.

The assistant is defined by ``vapi/assistant.json`` plus
``prompts/intake_agent.v1.md`` - not by clicking around the Vapi dashboard.
That keeps the prompt and tool schemas reviewable in the repo and makes the
deployment reproducible.

Usage:
    python -m vapi.provision                 # create or update the assistant
    python -m vapi.provision --list-numbers  # show phone numbers on the account
    python -m vapi.provision --attach <phone_number_id>
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "vapi" / "assistant.json"
PROMPT = ROOT / "prompts" / "intake_agent.v1.md"
STATE = ROOT / ".vapi-assistant.json"

API = "https://api.vapi.ai"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit("{} is not set. Add it to your .env file.".format(name))
    return value


def _extract_prompt() -> str:
    """Pull the text between the PROMPT BEGINS / PROMPT ENDS markers.

    The markdown file also contains design rationale for human readers; only
    the marked section is sent to the model.
    """
    text = PROMPT.read_text(encoding="utf-8")
    start = text.find("## PROMPT BEGINS")
    end = text.find("## PROMPT ENDS")
    if start == -1 or end == -1:
        sys.exit("Could not find PROMPT BEGINS/ENDS markers in {}".format(PROMPT))
    return text[start + len("## PROMPT BEGINS"):end].strip()


def build_payload() -> dict:
    base_url = _env("PUBLIC_BASE_URL").rstrip("/")
    secret = _env("VAPI_SERVER_SECRET")

    raw = CONFIG.read_text(encoding="utf-8")
    raw = raw.replace("__BASE_URL__", base_url)
    payload = json.loads(raw)

    payload["model"]["messages"][0]["content"] = _extract_prompt()

    # Attach the shared secret to every server endpoint so the API can
    # authenticate tool calls. Kept out of assistant.json so no secret is
    # ever committed.
    headers = {"X-Vapi-Secret": secret}
    for tool in payload["model"].get("tools", []):
        tool.setdefault("server", {})["headers"] = headers
    payload.setdefault("server", {})["headers"] = headers

    return payload


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": "Bearer {}".format(api_key)},
        timeout=30.0,
    )


def provision() -> None:
    api_key = _env("VAPI_API_KEY")
    payload = build_payload()

    existing_id = None
    if STATE.exists():
        existing_id = json.loads(STATE.read_text()).get("assistant_id")

    with _client(api_key) as client:
        if existing_id:
            resp = client.patch("/assistant/{}".format(existing_id), json=payload)
            action = "updated"
        else:
            resp = client.post("/assistant", json=payload)
            action = "created"

        if resp.status_code >= 400:
            print("Vapi returned {}:".format(resp.status_code))
            print(resp.text)
            sys.exit(1)

        data = resp.json()

    STATE.write_text(json.dumps({"assistant_id": data["id"]}, indent=2))
    print("Assistant {}: {}".format(action, data["id"]))
    print("Tools point at: {}".format(_env("PUBLIC_BASE_URL")))
    print("\nNext: attach a phone number with")
    print("  python -m vapi.provision --list-numbers")
    print("  python -m vapi.provision --attach <phone_number_id>")


def list_numbers() -> None:
    with _client(_env("VAPI_API_KEY")) as client:
        resp = client.get("/phone-number")
        resp.raise_for_status()
        numbers = resp.json()

    if not numbers:
        print("No phone numbers on this account. Buy one in the Vapi "
              "dashboard (Phone Numbers -> Buy Number), then re-run this.")
        return

    for n in numbers:
        print("{}  {}  assistant={}".format(
            n.get("id"), n.get("number"), n.get("assistantId")
        ))


def attach(phone_number_id: str) -> None:
    if not STATE.exists():
        sys.exit("No assistant provisioned yet. Run without --attach first.")
    assistant_id = json.loads(STATE.read_text())["assistant_id"]

    with _client(_env("VAPI_API_KEY")) as client:
        resp = client.patch(
            "/phone-number/{}".format(phone_number_id),
            json={"assistantId": assistant_id},
        )
        if resp.status_code >= 400:
            print(resp.text)
            sys.exit(1)
        print("Attached {} to assistant {}".format(
            resp.json().get("number"), assistant_id
        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-numbers", action="store_true")
    parser.add_argument("--attach", metavar="PHONE_NUMBER_ID")
    args = parser.parse_args()

    if args.list_numbers:
        list_numbers()
    elif args.attach:
        attach(args.attach)
    else:
        provision()
