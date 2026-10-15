import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
# Separate launch URL for BPSC Mains AnswerLab. This is a UI URL, not the evaluator API base.
ANSWERLAB_URL = os.getenv(
    "ANSWERLAB_URL",
    "https://bpscnexus-ai-evaluator.onrender.com",
).strip()
EVALUATOR_URL = os.getenv("EVALUATOR_URL", "").rstrip("/")
telegram_app: Application | None = None
polling_task: asyncio.Task | None = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "📚 *BPSC NEXUS*\\n\\n"
        "Choose the workspace you want to open."
    )
    keyboard = []
    if MINI_APP_URL:
        keyboard.append([InlineKeyboardButton(
            "📚 BPSC Nexus TestBot",
            web_app=WebAppInfo(url=MINI_APP_URL),
        )])
    if ANSWERLAB_URL:
        keyboard.append([InlineKeyboardButton(
            "✍️ BPSC Mains AnswerLab",
            web_app=WebAppInfo(url=ANSWERLAB_URL),
        )])
    await update.effective_message.reply_text(
        message,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📖 BPSC Nexus commands\\n\\n/start — Choose TestBot or Mains AnswerLab\\n"
        "/help — Show commands\\n/health — Check bot configuration"
    )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot runtime is active ✅\\n"
        f"TestBot Mini App URL: {'configured' if MINI_APP_URL else 'not configured'}\\n"
        f"Mains AnswerLab URL: {'configured' if ANSWERLAB_URL else 'not configured'}\\n"
        f"Evaluator API URL: {'configured' if EVALUATOR_URL else 'not configured'}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, polling_task
    if BOT_TOKEN:
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("health", health_command))
        await telegram_app.initialize()
        await telegram_app.start()
        if telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=False)
            logging.info("Telegram polling started.")
    else:
        logging.warning("TELEGRAM_BOT_TOKEN missing; web app starts but Telegram bot is disabled.")
    try:
        yield
    finally:
        if telegram_app:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()


app = FastAPI(title="BPSC Nexus Telegram Mini App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def home():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "telegram_configured": bool(BOT_TOKEN),
        "mini_app_url_configured": bool(MINI_APP_URL),
        "answerlab_url_configured": bool(ANSWERLAB_URL),
        "evaluator_configured": bool(EVALUATOR_URL),
    }


@app.get("/api/config")
async def public_config():
    # Never expose bot tokens or other secrets to the browser.
    return {"evaluator_configured": bool(EVALUATOR_URL)}


@app.post("/api/evaluate")
async def evaluate(payload: dict[str, Any]):
    """Minimal proxy. Verify the existing evaluator's request schema before relying on this."""
    if not EVALUATOR_URL:
        raise HTTPException(status_code=503, detail="Evaluator API is not configured.")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f"{EVALUATOR_URL}/api/evaluate", json=payload)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Evaluator returned HTTP {response.status_code}. Check its expected request format.",
            )
        if "application/json" in content_type:
            return JSONResponse(status_code=response.status_code, content=response.json())
        return {"result": response.text}
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Evaluator request timed out.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach evaluator service.") from exc
