# Telegram Bot for BTC Seed Phrase Audit
# Python 3.10+ | aiogram 3.x

import os
import re
import json
import logging
import asyncio
from typing import List, Tuple, Dict, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, BufferedInputFile

import aiohttp
from bip_utils import Bip39SeedGenerator, Bip84, Bip84Coins, Bip39Validator

BOT_TOKEN = "8966599826:AAE_DwGBZWRYhuiuc6Jy0X4kTwVeuC3a7jQ"
ADMIN_ID = 7916504148

MEMPOOL_API_URL = "https://mempool.space/api"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def parse_seed_line(line: str) -> List[str]:
    cleaned = line.strip()
    if cleaned.startswith('['): cleaned = cleaned[1:]
    if cleaned.endswith(']'): cleaned = cleaned[:-1]
    words = [w.strip(" '\"\t\r\n") for w in re.split(r'[, \t]+', cleaned) if w.strip(" '\"\t\r\n")]
    return words

def seed_to_btc_address(words: List[str]) -> str:
    mnemonic = " ".join(words)
    validator = Bip39Validator()
    if not validator.IsValid(mnemonic):
        raise ValueError("Невалидная мнемоническая фраза")
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip84_mst = Bip84.FromSeed(seed_bytes, Bip84Coins.BITCOIN)
    bip84_acc = bip84_mst.Purpose().Coin().Account(0).ChainExternal().AddressIndex(0)
    return bip84_acc.PublicKey().ToAddress()

async def get_btc_price(session: aiohttp.ClientSession) -> float:
    try:
        async with session.get(BINANCE_PRICE_URL, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("price", 0))
    except Exception as e:
        logging.error(f"Error fetching BTC price: {e}")
    return 0.0

async def get_address_stats(session: aiohttp.ClientSession, address: str) -> Dict[str, Any]:
    url = f"{MEMPOOL_API_URL}/address/{address}"
    async with session.get(url, timeout=15) as resp:
        if resp.status != 200:
            raise Exception(f"HTTP Error {resp.status}")
        data = await resp.json()
        chain = data.get("chain_stats", {})
        funded = chain.get("funded_txo_sum", 0)
        spent = chain.get("spent_txo_sum", 0)
        return {
            "balance_btc": (funded - spent) / 1e8,
            "turnover_btc": (funded + spent) / 1e8
        }

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👋 Привет, Админ!\nОтправь мне файл `seed.txt` с сид-фразами.")

@dp.message(F.document)
async def handle_document(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    doc = message.document
    if not doc.file_name.endswith(('.txt', '.seed')):
        await message.answer("⚠️ Пожалуйста, отправьте файл формата `.txt`.")
        return

    status_msg = await message.answer("📥 Файл получен. Начинаю обработку...")
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    content = file_bytes.read().decode('utf-8', errors='ignore')

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    total_seeds = len(lines)

    if total_seeds == 0:
        await status_msg.edit_text("❌ Файл пуст.")
        return

    await status_msg.edit_text(f"⏳ Найдено строк: {total_seeds}.\nПолучение данных...")

    count_gt_1000 = 0
    count_gt_100 = 0
    count_gt_1 = 0
    count_zero = 0
    count_errors = 0
    report_lines = []

    async with aiohttp.ClientSession() as session:
        btc_price = await get_btc_price(session)

        for idx, line in enumerate(lines, 1):
            words = parse_seed_line(line)
            if len(words) not in (12, 15, 18, 21, 24):
                count_errors += 1
                report_lines.append(f"[[{','.join(words)}], ОШИБКА: Неверное кол-во слов, 0 BTC]")
                continue

            try:
                address = seed_to_btc_address(words)
                stats = await get_address_stats(session, address)
                balance_btc = stats["balance_btc"]
                turnover_btc = stats["turnover_btc"]
                balance_usd = balance_btc * btc_price

                if balance_usd >= 1000:
                    count_gt_1000 += 1
                elif balance_usd >= 100:
                    count_gt_100 += 1
                elif balance_usd >= 1:
                    count_gt_1 += 1
                else:
                    count_zero += 1

                formatted_seed = f"[{','.join(words)}]"
                report_lines.append(f"[{formatted_seed}, {balance_btc:.8f} BTC (${balance_usd:.2f}), {turnover_btc:.8f} BTC]")
            except Exception as e:
                count_errors += 1
                report_lines.append(f"[[{','.join(words)}], ОШИБКА: {str(e)}, 0 BTC]")

            await asyncio.sleep(0.3)

            if idx % 10 == 0 or idx == total_seeds:
                try:
                    await status_msg.edit_text(f"⏳ Обработано {idx}/{total_seeds}...")
                except Exception:
                    pass

    summary_text = (
        f"📊 **Результаты анализа:**\n\n"
        f"🔹 Всего кошельков: **{total_seeds}**\n"
        f"💰 Баланс > 1000$: **{count_gt_1000}**\n"
        f"💵 Баланс > 100$: **{count_gt_100}**\n"
        f"🪙 Баланс > 1$: **{count_gt_1}**\n"
        f"⚪ Без денег: **{count_zero}**\n"
        f"⚠️ Ошибок: **{count_errors}**\n\n"
        f"💵 Курс BTC: **${btc_price:,.2f}**"
    )

    report_file_content = "\n".join(report_lines)
    document_output = BufferedInputFile(
        bytes(report_file_content, encoding='utf-8'), 
        filename="report_seed.txt"
    )

    await message.answer_document(document=document_output, caption=summary_text, parse_mode="Markdown")

if __name__ == "__main__":
    asyncio.run(main())
