# Restaurant Telegram Bot

A Telegram bot for restaurants: customers can browse the menu, check opening hours and book a table in a few taps. The owner gets a Telegram message for every new or cancelled booking.

The bot talks to customers in Italian. Code and docs are in English.

## Features

- **Main menu with buttons**: Menù · Prenota un tavolo · Orari e indirizzo · Le mie prenotazioni
- **Menu** with categories and prices, read from `menu.json` (no code changes needed to edit it)
- **Guided booking** in 5 steps: name → number of people → date (*Oggi*, *Domani* or a typed date like `25/12`) → time slot → notes, then a summary to confirm
  - Rejects past dates, closed days and time slots that have already passed today
  - `/annulla` (or the ❌ button) cancels at any step
- **Owner notifications**: new and cancelled bookings are sent to the owner's chat (`OWNER_CHAT_ID`)
- **`/mioid`** replies with your chat id, so the owner can find theirs
- **My bookings**: customers see their upcoming bookings and can cancel them (with a confirmation step)
- **Clear replies to unexpected input**: stickers, photos, random text or unknown commands get a helpful answer instead of silence
- Bookings stored in a local **SQLite** database (`bookings.db`, created automatically)

## Screenshots

| Main menu | Booking | Owner notification |
|---|---|---|
| ![Main menu](docs/screenshot-start.png) | ![Booking](docs/screenshot-booking.png) | ![Owner notification](docs/screenshot-owner.png) |

*Placeholder: add your screenshots to the `docs/` folder.*

## Installation

You need Python 3.10+ and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone <repo-url> restaurant-telegram-bot && cd restaurant-telegram-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then set TELEGRAM_TOKEN in .env
python bot.py
```

### Setting up owner notifications

1. Start the bot and send it `/mioid` from the owner's Telegram account.
2. Copy the number into `.env` as `OWNER_CHAT_ID=...`.
3. Restart the bot.

The owner must have sent at least one message to the bot, otherwise Telegram does not let the bot write to them.

## Configuration

### `.env`

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | yes | Bot token from @BotFather |
| `OWNER_CHAT_ID` | no | Chat that receives booking notifications |
| `TIMEZONE` | no | Default `Europe/Rome`; used for "today" and past time slots |

### Changing the menu

Edit the `menu` section of `menu.json`. Each category has a name, an emoji and a list of dishes:

```json
{
  "category": "Primi",
  "emoji": "🍝",
  "items": [
    {"name": "Spaghetti alla carbonara", "description": "Guanciale, pecorino, uovo", "price": 12.00}
  ]
}
```

You can add, remove or reorder categories and dishes. `description` can be an empty string. Prices use a dot as the decimal separator (`12.50`); the bot shows them as `€ 12,50`.

**No restart needed**: the file is re-read every time someone opens the menu. If the file contains a JSON error, the bot keeps using the last valid version and logs the error.

### Changing opening hours, address and time slots

Edit the `restaurant` section of `menu.json`:

| Field | What it does |
|---|---|
| `name`, `address`, `phone`, `maps_url` | Shown in "Orari e indirizzo" |
| `opening_hours` | List of lines shown to the customer, as free text |
| `closed_weekdays` | Days when booking is not allowed: `0` = Monday … `6` = Sunday |
| `booking_times` | Bookable time slots, in `HH:MM` format |
| `max_people` | Maximum party size for an online booking |

Tip: check the file with a JSON validator (e.g. jsonlint.com) after editing.

## Project structure

```
bot.py            # Telegram handlers and booking conversation
database.py       # SQLite storage for bookings
menu.json         # Menu, opening hours, address, time slots
requirements.txt
.env.example      # Template for .env (the real .env is git-ignored)
```

## Commands

| Command | Description |
|---|---|
| `/start` | Main menu |
| `/prenota` | Book a table |
| `/menu` | Show the menu |
| `/info` | Opening hours and address |
| `/prenotazioni` | My bookings |
| `/annulla` | Cancel the booking in progress |
| `/mioid` | Show your chat id |
| `/help` | Help |
