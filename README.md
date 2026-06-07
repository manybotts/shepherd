# Telegram Force-Sub Manager

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/manybotts/shepherd)

This is a small Telegram manager bot for people running many file-store bots.

It now uses MongoDB-compatible storage and supports isolated workspaces. You can share the same deployed manager bot with friends; each Telegram user gets their own tenant keyed to their Telegram user ID. Their bots, channels, and API key do not touch yours.

It gives you two modes:

1. **Central force-sub API, recommended**: only this manager bot needs to be admin in a channel. File-store bots call `POST /api/check-subscription` with that user's personal API key.
2. **Promotion helper**: register bot user IDs and run `/sync @channel` to promote registered bots in a channel when Telegram allows it.

Telegram's Bot API can promote/demote users in a supergroup/channel only when the manager bot is already an admin with the right permissions. In practice, promotion may still fail with `participant not found` if the target bot is not already known/member in that chat. That is why the central API mode is safer.

## Files

- `main.py` - FastAPI app, Telegram webhook, MongoDB storage, commands, and force-sub API.
- `requirements.txt` - Python dependencies.
- `.env.example` - environment variables to copy.
- `render.yaml` - quick Render blueprint.
- `client_example.py` - tiny example for file-store bots.

## Environment

Required:

- `BOT_TOKEN` - token from BotFather.
- `OWNER_IDS` - your Telegram numeric user ID. Multiple IDs can be comma separated.
- `MONGODB_URI` - MongoDB Atlas or another MongoDB-compatible connection URI.

Recommended:

- `MONGODB_DB=force_sub_manager`
- `INVITE_CODE` - code friends must send with `/invite CODE` to activate their own workspace.
- `ALLOW_SELF_SERVICE=false` - keep false unless you want any Telegram user to create a workspace.
- `WEBHOOK_SECRET` - random string for Telegram webhook verification.
- `PUBLIC_URL` - deployed app URL, for example `https://your-app.onrender.com`.

Optional:

- `API_SECRET` - legacy global API key. Personal `/apikey` keys are better for sharing.
- `CHANNELS` - comma separated channels to seed into the `OWNER_IDS` workspace on first boot.

## Create the manager bot

1. Open [@BotFather](https://t.me/BotFather).
2. Create a bot and copy the token.
3. Run the app locally once and send `/whoami` to your manager bot to get your Telegram numeric user ID.
4. Put that ID in `OWNER_IDS`.

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env`, then in PowerShell:

```powershell
$env:BOT_TOKEN="123456:your-token"
$env:OWNER_IDS="123456789"
$env:MONGODB_URI="mongodb+srv://user:pass@cluster.example.mongodb.net/?retryWrites=true&w=majority"
$env:INVITE_CODE="share-this-with-friends"
python main.py poll
```

Send `/help` to the bot.

## Sharing With Friends

Your own setup is created automatically from `OWNER_IDS`.

For a friend:

1. Send them the manager bot username.
2. Send them the invite code.
3. They send `/invite CODE` to the bot.
4. They run `/apikey rotate` and use that key in their own file-store bots.
5. They add their own bots/channels with `/addbot`, `/addchannel`, and `/sync`.

Their workspace is their Telegram user ID. Your workspace is your Telegram user ID. The database records are separated by `tenant_id`.

## Commands

`/whoami` shows user ID, chat ID, and tenant status.

`/workspace` shows current tenant ID, bot count, channel count, and API key status.

`/apikey` shows whether an API key exists. `/apikey rotate` creates a fresh personal API key.

`/addbot 123456789 @SomeFileBot` registers a file-store bot in your workspace. Username alone is not enough because the Bot API needs the numeric user ID. You can also forward a message from the target bot to the manager bot; if Telegram includes the forward origin, the manager will auto-register it.

`/addchannel @mychannel` saves a public channel in your workspace. For private channels, admin the manager bot in the channel first; it records the numeric channel ID automatically from Telegram's `my_chat_member` update.

`/checkadmin @mychannel` confirms the manager has admin rights.

`/sync @mychannel` tries to promote every bot registered in your workspace.

`/admins @mychannel` lists visible bot admins.

## Recommended Force-Sub API Mode

Admin only the manager bot in your force-sub channel. Give it enough rights to call `getChatMember`.

Then, in a file-store bot, call:

```http
POST https://your-service.example.com/api/check-subscription
X-API-Key: fsm_123456789_personal_key_from_apikey_rotate
Content-Type: application/json

{
  "user_id": 123456789,
  "channel": "@your_channel"
}
```

Response:

```json
{
  "ok": true,
  "tenant_id": "123456789",
  "subscribed": true,
  "mode": "all",
  "results": [
    {
      "channel": "@your_channel",
      "ok": true,
      "status": "member",
      "subscribed": true
    }
  ]
}
```

If you omit `channel`, the API checks all saved channels in that API key's workspace.

Use `channels` instead of `channel` if you require multiple channels:

```json
{
  "user_id": 123456789,
  "channels": ["@channel_one", "@channel_two"],
  "mode": "all"
}
```

## Deploy on Render

Render's free web services can host the bot, but free instances may sleep. MongoDB keeps the setup durable across restarts.

1. Push this folder's contents to a GitHub repo. `render.yaml` must be in the repo root.
2. Edit the deploy button at the top of this README and replace `https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME` with your repo URL.
3. Create a MongoDB Atlas database or another MongoDB-compatible database.
4. Click the deploy button.
5. Render will show prompt fields for the `sync: false` env vars:
   - `BOT_TOKEN`
   - `OWNER_IDS`
   - `MONGODB_URI`
   - `PUBLIC_URL`
   - optional `API_SECRET`
6. Render auto-generates:
   - `INVITE_CODE`
   - `WEBHOOK_SECRET`
7. Deploy. The app sets the Telegram webhook on startup.
8. Open `https://your-app.onrender.com/healthz`.

Manual Render setup also works:

1. Push this folder's contents to GitHub.
2. Create a MongoDB Atlas database or another MongoDB-compatible database.
3. Create a Render Web Service or use `render.yaml`.
4. Set:
   - `BOT_TOKEN`
   - `OWNER_IDS`
   - `MONGODB_URI`
   - `MONGODB_DB`
   - `INVITE_CODE`
   - `WEBHOOK_SECRET`
   - `PUBLIC_URL` to your Render service URL, for example `https://your-app.onrender.com`
5. Deploy. The app sets the Telegram webhook on startup.
6. Open `https://your-app.onrender.com/healthz`.

## What This Bot Cannot Magically Do

It cannot bypass Telegram permissions. The manager bot must be admin in the target channel/supergroup, and it needs the admin right to add/promote admins. If Telegram says `participant not found`, you still need to add the target bot once or switch to the central API mode.

It also cannot discover arbitrary bot user IDs from `@username` alone. Use `/whoami` with a forwarded message, or add the numeric ID manually.

## References

- [Telegram Bot API: promoteChatMember](https://core.telegram.org/bots/api#promotechatmember)
- [Telegram Bot API: getChatMember](https://core.telegram.org/bots/api#getchatmember)
- [Telegram Bot API: getChatAdministrators](https://core.telegram.org/bots/api#getchatadministrators)
- [Render free instance docs](https://render.com/docs/free)
