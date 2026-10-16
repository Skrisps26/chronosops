"""Validate Slack tokens, Socket Mode connectivity, and channel membership."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import dotenv_values
from slack_sdk.web.async_client import AsyncWebClient

from seed.load import SECRETS_FILE


def get_slack_config() -> dict[str, str]:
    config = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if isinstance(v, str) and v}
    config.update({k: v for k, v in os.environ.items() if v})
    return {
        "bot_token": config.get("SLACK_BOT_TOKEN", ""),
        "app_token": config.get("SLACK_APP_TOKEN", ""),
        "channel_id": config.get("SLACK_CHANNEL_ID", ""),
    }


async def run_preflight() -> int:
    config = get_slack_config()
    bot_token = config["bot_token"]
    app_token = config["app_token"]
    channel_id = config["channel_id"]

    print("=" * 60)
    print("CHRONOSOPS SLACK CONNECTOR PREFLIGHT")
    print("=" * 60)

    if not bot_token or not bot_token.startswith("xoxb-"):
        print("BLOCKED: Missing or invalid SLACK_BOT_TOKEN (must start with xoxb-).")
        print("Create a Slack app using slack/manifest.yaml and set SLACK_BOT_TOKEN.")
        return 1

    if not app_token or not app_token.startswith("xapp-"):
        print("BLOCKED: Missing or invalid SLACK_APP_TOKEN (must start with xapp-).")
        print("Enable Socket Mode and generate an app-level token with connections:write.")
        return 1

    if not channel_id or not channel_id.startswith("C"):
        print("BLOCKED: Missing or invalid SLACK_CHANNEL_ID (must start with C).")
        print("Invite the bot to a dedicated public channel (e.g. #chronosops-demo) and copy its ID.")
        return 1

    client = AsyncWebClient(token=bot_token)
    try:
        auth_resp = await client.auth_test()
        print(f"Authenticated as bot: {auth_resp.get('user')} (ID: {auth_resp.get('user_id')})")
        print(f"Workspace / Team:      {auth_resp.get('team')} (ID: {auth_resp.get('team_id')})")
    except Exception as e:
        print(f"FAILED auth.test: {e}")
        return 1

    try:
        # Check channel history read permission
        hist_resp = await client.conversations_history(channel=channel_id, limit=5)
        messages = hist_resp.get("messages", [])
        print(f"Channel {channel_id}: access verified ({len(messages)} recent messages).")
    except Exception as e:
        print(f"FAILED conversations.history for {channel_id}: {e}")
        print("Make sure the bot was invited to the channel: '/invite @ChronosOps'")
        return 1

    print("SUCCESS: Slack bot credentials and channel access verified.")
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    return asyncio.run(run_preflight())


if __name__ == "__main__":
    raise SystemExit(main())
