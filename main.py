import os, re, uuid
from pathlib import Path

import fitz  # PyMuPDF
from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TMP_DIR = Path("/tmp/tgpdfbot")
TMP_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()


# ---------- PDF functions ----------
def remove_all_links(in_path: Path, out_path: Path):
    doc = fitz.open(in_path)
    for page in doc:
        for lnk in page.get_links():
            page.delete_link(lnk)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()

def remove_links_from_page(in_path: Path, out_path: Path, page_num_1based: int):
    doc = fitz.open(in_path)
    page = doc[page_num_1based - 1]
    for lnk in page.get_links():
        page.delete_link(lnk)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()

def add_link_on_text(in_path: Path, out_path: Path, page_num_1based: int, text: str, url: str):
    doc = fitz.open(in_path)
    page = doc[page_num_1based - 1]
    rects = page.search_for(text)
    for r in rects:
        page.insert_link({"kind": fitz.LINK_URI, "from": r, "uri": url})
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()

def make_urls_clickable(in_path: Path, out_path: Path):
    # Simple approach: find visible "http(s)://..." strings and add URI links on top.
    url_re = re.compile(r"https?://\S+")
    doc = fitz.open(in_path)

    for page in doc:
        text = page.get_text("text")
        urls = set(url_re.findall(text))
        for url in urls:
            # Trim common trailing punctuation
            clean = url.rstrip(").,;]")
            rects = page.search_for(clean)
            for r in rects:
                page.insert_link({"kind": fitz.LINK_URI, "from": r, "uri": clean})

    doc.save(out_path, garbage=4, deflate=True)
    doc.close()


# ---------- UI helpers ----------
def action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Make URLs clickable", callback_data="A_MAKE_CLICKABLE")],
        [InlineKeyboardButton("Remove ALL links", callback_data="A_REMOVE_ALL")],
        [InlineKeyboardButton("Remove links from page", callback_data="A_REMOVE_PAGE")],
        [InlineKeyboardButton("Add link to page (by text)", callback_data="A_ADD_LINK_TEXT")],
    ])


# ---------- Bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Send me a PDF (<= 50MB).\n\n"
        "After you send it, I will show options:\n"
        "• Make URLs clickable\n"
        "• Remove all links\n"
        "• Remove links from a specific page\n"
        "• Add a link on a page (by matching text)",
    )

async def on_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return

    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a PDF file.")
        return

    # Store file_id to download later
    context.user_data["pdf_file_id"] = doc.file_id
    context.user_data["pdf_name"] = doc.file_name or "file.pdf"
    context.user_data["state"] = "WAIT_ACTION"

    await update.message.reply_text(
        f"PDF received: {context.user_data['pdf_name']}\nChoose what you want to do:",
        reply_markup=action_keyboard(),
    )

async def on_action_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if "pdf_file_id" not in context.user_data:
        await query.message.reply_text("Please send a PDF first.")
        return

    action = query.data
    context.user_data["action"] = action

    if action == "A_REMOVE_ALL":
        await query.message.reply_text("OK. Processing: removing all links…")
        await process_and_send(update, context)

    elif action == "A_MAKE_CLICKABLE":
        await query.message.reply_text("OK. Processing: making URLs clickable…")
        await process_and_send(update, context)

    elif action == "A_REMOVE_PAGE":
        context.user_data["state"] = "WAIT_PAGE_NUMBER"
        await query.message.reply_text("Send the page number (example: 3).")

    elif action == "A_ADD_LINK_TEXT":
        context.user_data["state"] = "WAIT_ADDLINK_PAGE"
        await query.message.reply_text("Send the page number where the text is (example: 2).")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")

    if state == "WAIT_PAGE_NUMBER":
        try:
            page = int(update.message.text.strip())
            context.user_data["page"] = page
        except ValueError:
            await update.message.reply_text("Please send a valid number, example: 3")
            return

        await update.message.reply_text(f"OK. Processing: removing links from page {page}…")
        await process_and_send(update, context)

    elif state == "WAIT_ADDLINK_PAGE":
        try:
            page = int(update.message.text.strip())
            context.user_data["page"] = page
        except ValueError:
            await update.message.reply_text("Please send a valid number, example: 2")
            return

        context.user_data["state"] = "WAIT_ADDLINK_TEXT"
        await update.message.reply_text("Now send the EXACT text that should become clickable (must exist on that page).")

    elif state == "WAIT_ADDLINK_TEXT":
        context.user_data["link_text"] = update.message.text.strip()
        context.user_data["state"] = "WAIT_ADDLINK_URL"
        await update.message.reply_text("Now send the URL (example: https://example.com)")

    elif state == "WAIT_ADDLINK_URL":
        url = update.message.text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await update.message.reply_text("URL must start with http:// or https://")
            return

        context.user_data["url"] = url
        await update.message.reply_text("OK. Processing: adding link…")
        await process_and_send(update, context)

    else:
        await update.message.reply_text("Send a PDF first, or type /start to begin.")


async def process_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Downloads the PDF to /tmp, applies selected action, sends back result.
    """
    file_id = context.user_data["pdf_file_id"]
    action = context.user_data.get("action")

    job_id = uuid.uuid4().hex
    in_path = TMP_DIR / f"in_{job_id}.pdf"
    out_path = TMP_DIR / f"out_{job_id}.pdf"

    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(in_path))

        if action == "A_REMOVE_ALL":
            remove_all_links(in_path, out_path)

        elif action == "A_REMOVE_PAGE":
            page = int(context.user_data["page"])
            remove_links_from_page(in_path, out_path, page)

        elif action == "A_ADD_LINK_TEXT":
            page = int(context.user_data["page"])
            text = context.user_data["link_text"]
            url = context.user_data["url"]
            add_link_on_text(in_path, out_path, page, text, url)

        elif action == "A_MAKE_CLICKABLE":
            make_urls_clickable(in_path, out_path)

        else:
            await (update.callback_query.message if update.callback_query else update.message).reply_text(
                "Unknown action. Type /start and try again."
            )
            return

        # Send result
        caption = "Done. Here is your processed PDF."
        if update.callback_query:
            chat = update.callback_query.message
        else:
            chat = update.message

        with open(out_path, "rb") as f:
            await chat.reply_document(document=f, filename="processed.pdf", caption=caption)

        # Reset state so user can pick again (optional)
        context.user_data["state"] = "WAIT_ACTION"

    except Exception as e:
        msg = f"Error: {type(e).__name__}: {e}"
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
    finally:
        # Cleanup temp files
        try:
            if in_path.exists(): in_path.unlink()
            if out_path.exists(): out_path.unlink()
        except Exception:
            pass


# Register handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CallbackQueryHandler(on_action_button))
tg_app.add_handler(MessageHandler(filters.Document.ALL, on_pdf))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


# ---------- FastAPI webhook ----------
@app.on_event("startup")
async def on_startup():
    # Important: start the PTB application for proper operation
    await tg_app.initialize()
    await tg_app.start()

@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.stop()
    await tg_app.shutdown()

@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if got != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
