#!/usr/bin/env python3
"""One-time setup: create Vapi assistant and phone number."""
import json
import sys
import httpx

sys.path.insert(0, ".")
from app.vapi_config import build_assistant_config

VAPI_API = "https://api.vapi.ai"


def main():
    api_key = input("Vapi API key: ").strip()
    server_url = input("Your server URL (e.g. https://abc.ngrok-free.dev/webhooks/vapi): ").strip()

    if not api_key or not server_url:
        print("Error: both values required")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # 1. Create assistant
    print("\n=== Creating Vapi assistant ===")
    config = build_assistant_config(server_url)
    print(f"Config: model={config['model']['model']}, voice={config['voice']['voiceId']}")
    print(f"Tools: {[t['function']['name'] for t in config['model']['tools']]}")

    resp = httpx.post(f"{VAPI_API}/assistant", headers=headers, json=config, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"Error creating assistant: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)

    assistant = resp.json()
    assistant_id = assistant["id"]
    print(f"Assistant created: {assistant_id}")

    # 2. Get or create phone number
    print("\n=== Setting up phone number ===")
    choice = input("Use (1) Vapi free number or (2) skip for now? [1/2]: ").strip()

    phone_number = None
    if choice == "1":
        phone_resp = httpx.post(
            f"{VAPI_API}/phone-number",
            headers=headers,
            json={
                "provider": "vapi",
                "assistantId": assistant_id,
            },
            timeout=30,
        )
        if phone_resp.status_code in (200, 201):
            phone_data = phone_resp.json()
            phone_number = phone_data.get("number") or phone_data.get("phoneNumber")
            print(f"Phone number assigned: {phone_number}")
        else:
            print(f"Phone number creation: {phone_resp.status_code} {phone_resp.text[:200]}")
            print("You can set up a number later in the Vapi dashboard.")

    # 3. Print config to add to .env
    print("\n" + "=" * 50)
    print("Add these to your leon/v8/.env file:")
    print("=" * 50)
    print(f"VAPI_ENABLED=true")
    print(f"VAPI_API_KEY={api_key}")
    print(f"VAPI_ASSISTANT_ID={assistant_id}")
    print(f"VAPI_SERVER_URL={server_url}")
    if phone_number:
        print(f"\nYour phone number: {phone_number}")
        print("Call this number to test!")
    print("=" * 50)


if __name__ == "__main__":
    main()
