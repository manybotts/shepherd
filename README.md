# Telegram Bot Shepherd

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/manybotts/shepherd)

A Telegram manager bot for people running multiple bots. When you add the shepherd bot to a channel, it automatically promotes all bots registered in your workspace as admins.

## Files

- `main.py` - FastAPI app, Telegram webhook, MongoDB storage, and commands.
- `requirements.txt` - Python dependencies.
- `.env.example` - environment variables to copy.
- `render.yaml` - quick Render blueprint.

## Environment

Required:

- `BOT_TOKEN` - token from BotFather.
- `OWNER_IDS` - your Telegram numeric user ID. Multiple IDs can be comma separated.
- `MONGODB_URI` - MongoDB Atlas or another MongoDB-compatible connection URI.

Recommended:

- `MONGODB_DB=bot_manager`
- `INVITE_CODE` - code friends must send with `/invite CODE` to activate their own workspace.
- `ALLOW_SELF_SERVICE=false` - keep false unless you want any Telegram user to create a workspace.
- `WEBHOOK_SECRET` - random string for Telegram webhook verification.
- `PUBLIC_URL` - deployed app URL, for example `https://your-app.onrender.com`.

Optional:

- `CHANNELS` - comma separated channels to seed into the `OWNER_IDS` workspace on first boot.
- `PROMOTE_CAN_POST_MESSAGES=true` - give promoted bots post permission (default false).
- `PROMOTE_CAN_EDIT_MESSAGES=true` - give promoted bots edit permission (default false).
- `PROMOTE_CAN_DELETE_MESSAGES=true` - give promoted bots delete permission (default false).

## Create the manager bot

1. Open [@BotFather](https://t.me/BotFather).
2. Create a bot and ensure it has the **Admin rights** privilege enabled.
3. Run the app locally once and send `/whoami` to your manager bot to get your Telegram numeric user ID.
4. Put that ID in `OWNER_IDS`.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`, then run with polling:

```bash
python main.py poll
```

Send `/help` to the bot.

## How It Works

1. **Register your bots**: Send `/addbot 123456789 @SomeBot` or forward a message from the target bot and reply with `/addbot`.
2. **Add the shepherd bot to a channel** as an admin with \"Add admins\" permission.
3. **Automatic promotion**: The shepherd bot saves the channel and immediately promotes all your registered bots as admins in that channel.
4. You can also manually use `/sync @channel` or `/syncall` at any time.

## Commands

- `/whoami` - show user ID, chat ID, and tenant status.
- `/workspace` - show current tenant ID, bot count, channel count.
- `/addbot 123456789 @SomeBot` - register a bot in your workspace.
- `/removebot @SomeBot` - unregister a bot.
- `/bots` - list registered bots.
- `/addchannel @channel` - save a channel for syncing.
- `/removechannel @channel` - remove a channel.
- `/channels` - list saved channels.
- `/sync @channel` - promote all your registered bots in that channel.
- `/syncall` - promote all your registered bots in all your saved channels.
- `/admins @channel` - list bot admins visible to the shepherd bot.
- `/demote @channel @SomeBot` - remove one registered bot's admin rights.
- `/checkadmin @channel` - check if the shepherd bot has admin rights.
- `/invite CODE` - activate a shared workspace.
- `/export` - export your bots and channels as JSON.

## Permissions

By default, promoted bots get these admin rights:

- `can_manage_chat: true`

Optional additional permissions (set via environment variables):

- `PROMOTE_CAN_POST_MESSAGES` - can post messages in channels
- `PROMOTE_CAN_EDIT_MESSAGES` - can edit messages
- `PROMOTE_CAN_DELETE_MESSAGES` - can delete messages

## Deploy on Render

1. Push this repo to GitHub.
2. Create a MongoDB Atlas database.
3. Create a Render Web Service or use `render.yaml`.
4. Set the required environment variables.
5. Deploy. The app sets the Telegram webhook on startup.

## Limitations

- The manager bot must be admin in the target channel with \"Add admins\" permission.
- If Telegram says `participant not found`, the target bot needs to be added to the channel first.

## References

- [Telegram Bot API: promoteChatMember](https://core.telegram.org/bots/api#promotechatmember)
- [Telegram Bot API: getChatAdministrators](https://core.telegram.org/bots/api#getchatadministrators)
