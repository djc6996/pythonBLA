import os
import logging
import asyncio
import requests
import base64
import urllib.parse
import threading
import time		 
from bs4 import BeautifulSoup
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

TOKEN_BOT = os.getenv("TOKEN")
LOGIN_URL = "https://www.blablachat.it/index.php?/login/"
SEARCH_URL = "https://www.blablachat.it/index.php?/filtro-home-search/"

POLLING_INTERVAL = 900  # 15 minuti in secondi


class MonitorBot:
    def __init__(self, token, username, password):
        self.session = requests.Session()
        self.username = username
        self.password = password
        self.token = token
        self.bot = Bot(token=token)
        self.monitors = {}  # (chat_id, city, age_min, age_max, username) -> {"last_status": str, "active": bool}
        self.lock = threading.Lock()

    def get_csrf_token(self):
        response = self.session.get(LOGIN_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        csrf_input = soup.find("input", {"name": "csrfKey"})
        if csrf_input:
            return csrf_input["value"]
        else:
            raise ValueError("Token CSRF non trovato")

    @staticmethod
    def encode_ref(url: str) -> str:
        # Codifica in bytes UTF-8
        url_bytes = url.encode('utf-8')

        # Codifica base64 e converte a stringa
        base64_bytes = base64.b64encode(url_bytes)
        base64_str = base64_bytes.decode('utf-8')

        # Percent-encode per URL (encode /, =, +, etc.)
        encoded_ref = urllib.parse.quote(base64_str)

        return encoded_ref

    def login(self):
        csrf_token = self.get_csrf_token()
        ref_encode = self.encode_ref(LOGIN_URL)
        payload = {
            "csrfKey": csrf_token,
			#"ref": "aHR0cHM6Ly93d3cuYmxhYmxhY2hhdC5pdC9pbmRleC5waHA%2FL2xvZ2luLz0%3D",																		   
            "ref": ref_encode,
            "auth": self.username,
            "password": self.password,
            "remember_me": "1",
            "_processLogin": "usernamepassword",
        }
        response = self.session.post(LOGIN_URL, data=payload)
        response.raise_for_status()
        if self.username.lower() in response.text.lower() or "logout" in response.text.lower():
            logging.info("Login riuscito.")
            return True
        else:
            logging.error("Login fallito.")
            return False

    def cerca_utente_online(self, city, gender, age_min, age_max, target_user):
        offset = 0
        while True:
            data = {
                "region": city,
                "gender": gender,
                "age_min": str(age_min),
                "age_max": str(age_max),
                "offset": str(offset),
            }
            headers = {
                "Referer": LOGIN_URL,
                "User-Agent": "Mozilla/5.0",
            }
            response = self.session.post(SEARCH_URL, data=data, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            cards = soup.find_all("div", class_="usrCard")
            if not cards:
                return False

            for card in cards:
                a_tag = card.find("a", class_="usrProfileLink")
                if a_tag and a_tag.text:
                    username = a_tag.text.strip()
                    if username.lower() == target_user.lower():
                        if card.find("span", class_="usrDot usrOnline") is not None:
                            return "online"
                        elif card.find("span", class_="usrDot usrClock") is not None:
                            return "recently_online"
                        else:
                            return "offline"

            offset += len(cards)

    def start_monitoring(self, chat_id, city, age_min, age_max, target_user, gender="donna"):
        key = (chat_id, city.lower(), age_min, age_max, target_user.lower())

        with self.lock:
            if key in self.monitors:
                return False  # già in monitoraggio
            self.monitors[key] = {"last_status": None, "active": True}

        def polling():
            if not self.login():
                with self.lock:
                    self.monitors[key]["active"] = False
                return
            
            start_time = time.time()  # MODIFICA: tempo di inizio
            
            while True:
                with self.lock:
                    if not self.monitors[key]["active"]:
                        break
                
                # MODIFICA: controllo tempo trascorso
                elapsed_time = time.time() - start_time
                if elapsed_time > 20 * 3600:  # 20 ore in secondi
                    logging.info(f"Monitoraggio per {target_user} terminato dopo 20 ore.")
                    with self.lock:
                        self.monitors[key]["active"] = False
                        del self.monitors[key]
                    break
                
                try:
                    status = self.cerca_utente_online(city, gender, age_min, age_max, target_user)

                    with self.lock:
                        last_status = self.monitors[key]["last_status"]

                        if status in ("online", "recently_online") and status != last_status:
                            if status == "online":
                                msg = f"📢 L'utente {target_user} è ora ONLINE in {city} ({age_min}-{age_max})!"
                            else:  # recently_online
                                msg = f"ℹ️ L'utente {target_user} era ONLINE poco fa in {city} ({age_min}-{age_max})."

                            logging.info(msg)
                            asyncio.run_coroutine_threadsafe(
                                self.bot.send_message(chat_id=chat_id, text=msg),
                                asyncio.get_event_loop(),
                            )
                        self.monitors[key]["last_status"] = status
                except Exception as e:
                    logging.error(f"Errore durante polling: {e}")
                time.sleep(POLLING_INTERVAL)

        thread = threading.Thread(target=polling, daemon=True)
        thread.start()
        return True

    def stop_monitoring(self, chat_id, city, age_min, age_max, target_user):
        key = (chat_id, city.lower(), age_min, age_max, target_user.lower())
        with self.lock:
            if key in self.monitors:
                self.monitors[key]["active"] = False
                del self.monitors[key]
                return True
            else:
                return False

    def stop_all_monitoring(self):
        with self.lock:
            for key in list(self.monitors.keys()):
                self.monitors[key]["active"] = False
            self.monitors.clear()

    def list_monitors(self, chat_id):
        with self.lock:
            active = [
                (city, age_min, age_max, user)
                for (c_id, city, age_min, age_max, user), v in self.monitors.items()
                if c_id == chat_id and v["active"]
            ]
        return active



# Handlers per Telegram bot

async def monitor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Uso corretto:\n/monitor <città> <età_min> <età_max> <nome_utente>\n\n"
            "Esempio:\n/monitor roma 25 35 utente"
        )
        return

    city, age_min_str, age_max_str, username = args
    try:
        age_min = int(age_min_str)
        age_max = int(age_max_str)
    except ValueError:
        await update.message.reply_text("Le età devono essere numeri interi.")
        return

    chat_id = update.effective_chat.id
    success = bot_instance.start_monitoring(chat_id, city, age_min, age_max, username)

    if success:
        await update.message.reply_text(
            f"Monitoraggio avviato per l'utente '{username}' a {city} ({age_min}-{age_max})."
        )
    else:
        await update.message.reply_text(
            "Monitoraggio già attivo per questi parametri."
        )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Uso corretto:\n/stop <città> <età_min> <età_max> <nome_utente>\n\n"
            "Esempio:\n/stop roma 25 35 utente"
        )
        return

    city, age_min_str, age_max_str, username = args
    try:
        age_min = int(age_min_str)
        age_max = int(age_max_str)
    except ValueError:
        await update.message.reply_text("Le età devono essere numeri interi.")
        return

    chat_id = update.effective_chat.id
    success = bot_instance.stop_monitoring(chat_id, city, age_min, age_max, username)

    if success:
        await update.message.reply_text(
            f"Monitoraggio fermato per l'utente '{username}' a {city} ({age_min}-{age_max})."
        )
    else:
        await update.message.reply_text(
            "Nessun monitoraggio attivo trovato per questi parametri."
        )


async def stopall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_instance.stop_all_monitoring()
    await update.message.reply_text("Tutti i monitoraggi sono stati fermati.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = bot_instance.list_monitors(chat_id)
    if not active:
        await update.message.reply_text("Nessun monitoraggio attivo.")
        return

    msg = "Monitoraggi attivi:\n"
    for city, age_min, age_max, user in active:
        msg += f"- {user} in {city} ({age_min}-{age_max})\n"
    await update.message.reply_text(msg)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Comandi disponibili:\n"
        "/monitor <città> <età_min> <età_max> <nome_utente> - Avvia il monitoraggio di un utente.\n"
        "/stop <città> <età_min> <età_max> <nome_utente> - Ferma il monitoraggio di un utente.\n"
        "/stopall - Ferma tutti i monitoraggi.\n"
        "/list - Mostra i monitoraggi attivi per questa chat.\n"
        "/help - Mostra questo messaggio di aiuto.\n\n"
        "Esempio:\n"
        "/monitor roma 25 35 utente\n"
        "/stop roma 25 35 utente"
    )
    await update.message.reply_text(help_text)


def main():
    application = ApplicationBuilder().token(TOKEN_BOT).build()

    application.add_handler(CommandHandler("monitor", monitor_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("stopall", stopall_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("help", help_command))

    print("Bot avviato.")
    application.run_polling()


if __name__ == "__main__":
    # Inizializza il bot con username e password di blablachat
    user_blablachat = os.getenv("USERNAME")
    pass_blablachat = os.getenv("PASSWORD")
    bot_instance = MonitorBot(TOKEN_BOT, user_blablachat, pass_blablachat)

    main()
