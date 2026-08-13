# Manga chatbot — Telegram, Discord, WhatsApp

A chat interface to a manga/manhwa catalogue, on the channels people actually
message from. Every answer is grounded in a retrieval backend that verifies its
own citations, and the bot hands off to a human rather than guessing when that
backend says it has nothing.

The interesting part is what this repo *doesn't* contain. There is no prompt
engineering here, no vector store, no retrieval logic. All of that lives in
[manhwa-rag](https://github.com/dogaozyildiz/manhwa-rag), which already scores
0.947 groundedness and refuses correctly on 5/5 out-of-corpus questions. This is
the channel layer over it — and keeping it a channel layer is the design.

```
Telegram (long polling) ──┐
Discord (slash commands) ─┼──► conversation.py ──┬─► rag.py ────► manhwa-rag POST /ask
WhatsApp (Cloud API) ─────┘   (channel-agnostic) │        └─ 503 ─► POST /retrieve
                                     │           │                  (no key needed)
                                     │           └─► communities.py ─► Reddit search API
                                     │
                                     ├──► store.py       SQLite: subscribers, escalations
                                     └──► escalation.py  refused → notify a human
```

`Conversation.handle(channel, user_id, text) -> Reply` is the whole contract.
Channels render a `Reply` and put it on the wire; they hold no logic. That is
why the WhatsApp adapter is one file and why the test suite needs neither a bot
token nor a network.

---

## What it does

**1. Grounded answers, with the source shown.**
The backend returns claims whose quotes have been checked to appear verbatim in
the source they name; unverifiable ones are stripped before they are ever sent
and counted. The bot renders the survivors as citations, and when the count is
non-zero it says so — *"1 unverifiable claim dropped before you saw this"*. The
verifier working is part of the product, not an implementation detail.

**2. Recommendations with real constraints.**
"recommend something like Solo Leveling under 100 chapters" is half SQL and half
embeddings. The backend splits it — `chapters < 100` becomes a `WHERE` clause
applied *before* ranking — and returns the predicates it used. The bot echoes
them back: *"Filtered on: Korean · finished · <100 chapters"*. Nothing over 100
chapters can come back, and the user can see why.

**3. Preference capture.**
`/subscribe` walks the user through which series they follow and an optional
contact, and stores it. The pending step lives in a database column rather than
process memory, so a restart mid-flow doesn't lose the thread.

**4. Community suggestions that are looked up, not invented.**
`/communities Solo Leveling` returns real subreddits with their current member
counts and descriptions, straight from Reddit's search API. This is the one
feature whose data the catalogue does not hold — it knows about series, not
about where people discuss them — so it was either going to be the project's
first hallucination or a lookup. A model asked to name subreddits will produce
plausible ones that do not exist. With no Reddit credentials configured the
command says so; it never falls back to guessing. And a Reddit outage reports
itself as an outage rather than as "no communities exist", because those are
different facts.

**5. Escalation that means something.**
`refused` is computed by the backend and covers three cases: nothing retrieved,
the model declined, or *every claim it made failed verification*. That last one
catches answers that looked fine and weren't. When it fires, the bot says it
can't answer, writes an escalation row, and pushes a notification to an
operator's chat.

---

## Running it

### 1. The backend

[manhwa-rag](https://github.com/dogaozyildiz/manhwa-rag) must be running. From
its directory:

```bash
docker compose up -d          # Postgres 17 + pgvector
alembic upgrade head
python -m app.ingest.pipeline # first run only, ~5 min
python -m scripts.serve       # http://127.0.0.1:8000
```

It takes 60–90 seconds to load its embedding model. `Application startup
complete` means ready. The bot starts fine before then and reports the backend
as still warming rather than crashing.

### 2. A Telegram bot token

Message [@BotFather](https://t.me/botfather), `/newbot`, copy the token. Free,
instant, no verification.

### 3. This bot

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env          # add TELEGRAM_BOT_TOKEN
python -m bot.telegram_main
```

Message it. That is the whole Telegram setup — long polling needs no public URL,
no tunnel and no TLS.

To have escalations pushed to you, set `ADMIN_CHAT_ID` to your own chat id
(message [@userinfobot](https://t.me/userinfobot) to find it). Left empty,
escalations are still recorded, just not pushed.

### 4. Discord (optional)

Create an application at [discord.com/developers](https://discord.com/developers/applications),
add a bot, copy the token into `DISCORD_BOT_TOKEN`. Free, no approval, and no
verification until the bot is in 100 servers.

Invite it with the `bot` and `applications.commands` scopes, put the server id
in `DISCORD_GUILD_ID` so commands appear instantly, then:

```bash
python -m bot.discord_main
```

`/ask`, `/communities`, `/subscribe`, `/status`, `/stop`, `/help`. Leave every
privileged intent switched **off** — the bot does not need any.

### 5. WhatsApp (optional)

```bash
uvicorn bot.api:app --port 8080
cloudflared tunnel --url http://localhost:8080     # or ngrok http 8080
```

In [Meta for Developers](https://developers.facebook.com/): create an app, add
the WhatsApp product, and you get a free test number and a token. Point the
webhook at `https://<your-tunnel>/webhook/whatsapp`, set the verify token to
whatever you put in `WHATSAPP_VERIFY_TOKEN`, and subscribe to `messages`. Add
your own number as a test recipient.

---

## Design notes

**Telegram uses HTML, not MarkdownV2.** MarkdownV2 requires escaping eighteen
characters wherever they appear, including `.`, `-` and `!`. Manga titles are
full of them, and one missed escape makes Telegram reject the entire message —
so the failure mode is the bot silently saying nothing. HTML has three escapes
and no such trap.

**Discord uses slash commands only, and that is a design choice.** Reading
ordinary message text needs the Message Content privileged intent, which must be
toggled on and re-applied for annually past 10,000 users. An interaction carries
its own arguments, so the bot runs on `Intents.none()` — no privileged intents at
all. The cost is that the multi-step `/subscribe` flow cannot span messages, so
Discord collects both answers as command parameters and feeds them through the
same state machine the other channels use rather than duplicating it.

**Every Discord command defers before working.** Discord discards an interaction
that is not acknowledged within 3 seconds, and a catalogue answer takes 2–4. So
each command calls `defer()` and replies through `followup`. There is a test
asserting the deferral happens *before* the slow call, because getting that
order wrong fails only under real latency.

**Message limits are platform facts, not settings.** Discord caps a message at
2000 characters — less than half of Telegram's 4096 — and rejects anything
longer outright instead of truncating. So the ceilings live in `format.py` keyed
by channel, and the body is trimmed to whatever the sources leave rather than
the sources being cut.

**The WhatsApp webhook returns 200 before doing the work.** Meta retries a
webhook it considers slow, and an answer takes 2–4 seconds. Answering
synchronously earns duplicate deliveries. Message ids are also remembered and
repeats dropped, because redelivery happens regardless.

**A 503 degrades to retrieval instead of an apology.** Generation needs a key
and the free Groq tier is 100k tokens a day; `/retrieve` takes an identical
request body and needs neither. So when the quota runs out the bot still returns
the matched catalogue entries and says which half of the system is down.

**Rate limiting lives here, not in the backend.** manhwa-rag is single-tenant
with no auth and its README warns against exposing it with a key attached. This
bot is that exposure, so it is the front door: a minimum gap between messages
stops hammering, and an hourly cap stops one patient user draining the day's
shared quota.

**`/stop` actually deletes.** A bot that captures contact details and only
pretends to forget them is a liability.

---

## Tests

```bash
pytest -q      # 164 tests
```

No network, no API keys, no database server. The backend and Reddit are both
`httpx.MockTransport` and the store is in-memory SQLite, so CI runs the real
conversation logic — the 503 fallback, redelivery suppression, HTML and markdown
escaping, token refresh, and Discord's defer-before-work ordering — without a
manhwa-rag or a bot token to talk to.

---

## What this does not do

- **WhatsApp runs on a Meta test number.** It can only message recipients you
  have verified, and free-form replies are limited to 24 hours after the user's
  last message. Going live on a real number needs business verification. Fine
  for a demo; stated rather than glossed.
- **Conversations are stateless.** Each question is answered on its own; there
  is no follow-up context, because the backend's `/ask` is stateless too.
- **Rate limiting is per-process.** A multi-replica deployment would need Redis.
- **Recommendation is the backend's weakest measured group** (recall@5 0.80).
  It is inherently one-to-many, and its golden set scores against an acceptable
  set rather than one right answer.
- **Alerts are captured, not sent.** `/subscribe` stores what a user follows;
  the job that watches for new chapters and notifies them is not built.
- **Discord *servers* are not recommended, only subreddits.** There is no
  sanctioned API for discovering servers a bot has not joined — Discord's own
  OpenAPI spec exposes 150 endpoints and every search route (`threads/search`,
  `members/search`, `messages/search`) operates inside a guild you are already
  in. Client-side Server Discovery runs on undocumented user-API endpoints that
  require a user token, which is self-botting and a terms violation. So the
  honest answer is subreddits, from live data, rather than a made-up server list.
- **Reddit API access needs approval.** Under Reddit's Responsible Builder
  Policy, access must be requested and granted before use. `/communities`
  degrades to explaining itself when credentials are absent.
- **Subreddit results are Reddit's search ranking**, not a taste model. The bot
  reports what Reddit returns, ordered by size.
