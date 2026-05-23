import requests
import sqlite3
import time
import logging
import asyncio
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURACIÓN
# ============================================================
TELEGRAM_TOKEN = "8498685576:AAGg7EKaW1UkOZRdUdArYAtzZfieovgNljI"
CHAT_ID = "1112728778"
CAPITAL_USDT = 300
SPREAD_MINIMO = 3.0
REP_MINIMA = 92
OPS_MINIMAS = 100
INTERVALO_MINUTOS = 5
META_MENSUAL_USD = 117

# ============================================================
# BASE DE DATOS
# ============================================================
def init_db():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS precios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            hora INTEGER,
            mejor_venta REAL,
            mejor_compra REAL,
            spread REAL,
            anunciantes_venta INTEGER,
            anunciantes_compra INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS operaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            monto_usdt REAL,
            precio_compra REAL,
            precio_venta REAL,
            spread REAL,
            ganancia_ars REAL,
            ganancia_usd REAL
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# SCRAPER BINANCE P2P
# ============================================================
def get_ofertas(trade_type):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "asset": "USDT",
        "fiat": "ARS",
        "merchantCheck": False,
        "page": 1,
        "payTypes": ["Mercadopago", "BankTransfer"],
        "publisherType": None,
        "rows": 20,
        "tradeType": trade_type
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        data = r.json()
        return data.get("data", [])
    except Exception as e:
        logging.error(f"Error consultando Binance P2P: {e}")
        return []

def filtrar_ofertas(ofertas):
    resultado = []
    for item in ofertas:
        adv = item.get("adv", {})
        advertiser = item.get("advertiser", {})
        try:
            precio = float(adv.get("price", 0))
            min_amount = float(adv.get("minSingleTransAmount", 0))
            max_amount = float(adv.get("maxSingleTransAmount", 999999999))
            completion_rate = float(advertiser.get("monthFinishRate", 0)) * 100
            total_orders = int(advertiser.get("monthOrderCount", 0))
            advertiser_no = advertiser.get("userNo", "")
            nickname = advertiser.get("nickName", "Desconocido")
            monto_ars = CAPITAL_USDT * precio
            if completion_rate < REP_MINIMA:
                continue
            if total_orders < OPS_MINIMAS:
                continue
            if monto_ars < min_amount or monto_ars > max_amount:
                continue
            resultado.append({
                "precio": precio,
                "nickname": nickname,
                "advertiser_no": advertiser_no,
                "reputacion": completion_rate,
                "operaciones": total_orders,
                "min_ars": min_amount,
                "max_ars": max_amount,
                "link": f"https://p2p.binance.com/es/advertiserDetail?advertiserNo={advertiser_no}"
            })
        except:
            continue
    return resultado

def calcular_spread(precio_compra, precio_venta):
    return ((precio_venta - precio_compra) / precio_compra) * 100

def calcular_ganancia(monto_usdt, precio_compra, precio_venta):
    ganancia_ars = monto_usdt * (precio_venta - precio_compra)
    ganancia_usd = ganancia_ars / precio_venta
    return ganancia_ars, ganancia_usd

# ============================================================
# STATS
# ============================================================
def get_stats_generales():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("SELECT AVG(spread) FROM precios WHERE timestamp > datetime('now', '-1 hour') AND spread > 0")
    avg_1h = c.fetchone()[0] or 0
    c.execute("SELECT AVG(spread) FROM precios WHERE timestamp > datetime('now', '-24 hours') AND spread > 0")
    avg_24h = c.fetchone()[0] or 0
    c.execute("SELECT AVG(spread) FROM precios WHERE timestamp > datetime('now', '-7 days') AND spread > 0")
    avg_7d = c.fetchone()[0] or 0
    conn.close()
    return avg_1h, avg_24h, avg_7d

def get_resumen_mes():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    mes_inicio = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    c.execute("""
        SELECT COUNT(*), SUM(ganancia_ars), SUM(ganancia_usd), MAX(spread), AVG(spread)
        FROM operaciones WHERE timestamp >= ?
    """, (mes_inicio,))
    row = c.fetchone()
    conn.close()
    return {
        "total_ops": row[0] or 0,
        "ganancia_ars": row[1] or 0,
        "ganancia_usd": row[2] or 0,
        "mejor_spread": row[3] or 0,
        "spread_promedio": row[4] or 0
    }

# ============================================================
# ALERTA
# ============================================================
async def enviar_alerta_async(vendedor, comprador, spread, ganancia_ars, ganancia_usd):
    avg_1h, avg_24h, avg_7d = get_stats_generales()
    resumen = get_resumen_mes()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)

    msg = (
        f"🟢 *OPORTUNIDAD DE ARBITRAJE*\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}hs\n\n"
        f"*PASO 1 — COMPRÁ primero:*\n"
        f"👤 {vendedor['nickname']}\n"
        f"⭐ {vendedor['reputacion']:.1f}% ({vendedor['operaciones']} ops)\n"
        f"💰 ${vendedor['precio']:,.2f} ARS/USDT\n"
        f"🔗 [Abrir en Binance]({vendedor['link']})\n\n"
        f"*PASO 2 — VENDÉ inmediatamente:*\n"
        f"👤 {comprador['nickname']}\n"
        f"⭐ {comprador['reputacion']:.1f}% ({comprador['operaciones']} ops)\n"
        f"💰 ${comprador['precio']:,.2f} ARS/USDT\n"
        f"🔗 [Abrir en Binance]({comprador['link']})\n\n"
        f"💵 *Capital:* {CAPITAL_USDT} USDT\n"
        f"📈 *Spread:* {spread:.2f}%\n"
        f"🏦 *Ganancia:* ${ganancia_ars:,.0f} ARS (~${ganancia_usd:.1f} USD)\n\n"
        f"📊 *Estadística:*\n"
        f"   Última hora: {avg_1h:.2f}%\n"
        f"   24hs: {avg_24h:.2f}%\n"
        f"   7 días: {avg_7d:.2f}%\n\n"
        f"🎯 *Meta mensual:*\n"
        f"   {barra_str} {progreso:.0f}%\n"
        f"   ${resumen['ganancia_usd']:.1f} / ${META_MENSUAL_USD} USD\n\n"
        f"⏳ _Actuá rápido — ventana de 5 a 10 min_\n"
        f"📝 _Registrá con /operacion monto compra venta_"
    )

    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=False
    )

def enviar_alerta(vendedor, comprador, spread, ganancia_ars, ganancia_usd):
    asyncio.run(enviar_alerta_async(vendedor, comprador, spread, ganancia_ars, ganancia_usd))

# ============================================================
# ESCANEO
# ============================================================
def escanear():
    logging.info(f"Escaneando... {datetime.now().strftime('%H:%M:%S')}")
    try:
        ofertas_venta = get_ofertas("BUY")
        ofertas_compra = get_ofertas("SELL")
        vendedores = filtrar_ofertas(ofertas_venta)
        compradores = filtrar_ofertas(ofertas_compra)

        if not vendedores or not compradores:
            logging.info("Sin ofertas válidas")
            return

        mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
        mejor_comprador = max(compradores, key=lambda x: x["precio"])
        spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
        ganancia_ars, ganancia_usd = calcular_ganancia(CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"])

        conn = sqlite3.connect("p2p_data.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO precios (timestamp, hora, mejor_venta, mejor_compra, spread, anunciantes_venta, anunciantes_compra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), datetime.now().hour, mejor_vendedor["precio"],
              mejor_comprador["precio"], max(spread, 0), len(vendedores), len(compradores)))
        conn.commit()
        conn.close()

        logging.info(f"Spread: {spread:.2f}%")
        if spread >= SPREAD_MINIMO:
            logging.info("¡Oportunidad encontrada!")
            enviar_alerta(mejor_vendedor, mejor_comprador, spread, ganancia_ars, ganancia_usd)
    except Exception as e:
        logging.error(f"Error en escaneo: {e}")

# ============================================================
# COMANDOS TELEGRAM
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot P2P Arbitraje ARS/USDT activo\\!*\n\n"
        "Comandos:\n"
        "/estado \\— ver mercado ahora\n"
        "/resumen \\— estadísticas del mes\n"
        "/historial \\— últimas operaciones\n"
        "/operacion \\— registrar operación\n"
        "/ayuda \\— instrucciones",
        parse_mode="MarkdownV2"
    )

async def cmd_operacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "Formato: /operacion MONTO PRECIO_COMPRA PRECIO_VENTA\n"
                "Ejemplo: /operacion 300 1000 1032"
            )
            return
        monto = float(args[0])
        precio_compra = float(args[1])
        precio_venta = float(args[2])
        spread = calcular_spread(precio_compra, precio_venta)
        ganancia_ars, ganancia_usd = calcular_ganancia(monto, precio_compra, precio_venta)

        conn = sqlite3.connect("p2p_data.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO operaciones (timestamp, monto_usdt, precio_compra, precio_venta, spread, ganancia_ars, ganancia_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), monto, precio_compra, precio_venta, spread, ganancia_ars, ganancia_usd))
        conn.commit()
        conn.close()

        resumen = get_resumen_mes()
        progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
        barras = int(progreso / 10)
        barra_str = "█" * barras + "░" * (10 - barras)
        faltante = max(0, META_MENSUAL_USD - resumen["ganancia_usd"])

        await update.message.reply_text(
            f"✅ OPERACIÓN REGISTRADA\n\n"
            f"Esta operación:\n"
            f"  Spread: {spread:.2f}%\n"
            f"  Ganancia: ${ganancia_ars:,.0f} ARS (~${ganancia_usd:.2f} USD)\n\n"
            f"Resumen del mes:\n"
            f"  Operaciones: {resumen['total_ops']}\n"
            f"  Total ganado: ${resumen['ganancia_usd']:.2f} USD\n\n"
            f"Meta ${META_MENSUAL_USD} USD:\n"
            f"  {barra_str} {progreso:.0f}%\n"
            f"  Falta: ${faltante:.2f} USD"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resumen = get_resumen_mes()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)
    await update.message.reply_text(
        f"📊 RESUMEN DEL MES\n\n"
        f"Operaciones: {resumen['total_ops']}\n"
        f"Ganancia: ${resumen['ganancia_usd']:.2f} USD\n"
        f"En pesos: ${resumen['ganancia_ars']:,.0f} ARS\n"
        f"Mejor spread: {resumen['mejor_spread']:.2f}%\n\n"
        f"Meta ${META_MENSUAL_USD} USD:\n"
        f"  {barra_str} {progreso:.0f}%"
    )

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("SELECT timestamp, monto_usdt, spread, ganancia_usd FROM operaciones ORDER BY timestamp DESC LIMIT 10")
    ops = c.fetchall()
    conn.close()
    if not ops:
        await update.message.reply_text("No hay operaciones registradas aún.")
        return
    texto = "📋 ÚLTIMAS OPERACIONES\n\n"
    for op in ops:
        fecha = datetime.fromisoformat(op[0]).strftime("%d/%m %H:%M")
        texto += f"• {fecha} | {op[1]:.0f} USDT | {op[2]:.1f}% | +${op[3]:.2f} USD\n"
    await update.message.reply_text(texto)

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Consultando mercado...")
    try:
        ofertas_venta = get_ofertas("BUY")
        ofertas_compra = get_ofertas("SELL")
        vendedores = filtrar_ofertas(ofertas_venta)
        compradores = filtrar_ofertas(ofertas_compra)
        if not vendedores or not compradores:
            await update.message.reply_text("Sin ofertas válidas con los filtros actuales.")
            return
        mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
        mejor_comprador = max(compradores, key=lambda x: x["precio"])
        spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
        _, ganancia_usd = calcular_ganancia(CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"])
        avg_1h, avg_24h, avg_7d = get_stats_generales()
        estado = "🟢 HAY OPORTUNIDAD" if spread >= SPREAD_MINIMO else "🔴 Sin oportunidad"
        await update.message.reply_text(
            f"📡 ESTADO DEL MERCADO\n\n"
            f"{estado}\n\n"
            f"Mejor compra: ${mejor_vendedor['precio']:,.2f} ARS\n"
            f"Mejor venta: ${mejor_comprador['precio']:,.2f} ARS\n"
            f"Spread actual: {spread:.2f}%\n"
            f"Ganancia est: ${ganancia_usd:.2f} USD\n\n"
            f"Promedios:\n"
            f"  Última hora: {avg_1h:.2f}%\n"
            f"  24hs: {avg_24h:.2f}%\n"
            f"  7 días: {avg_7d:.2f}%"
        )
    except Exception as e:
        await update.message.reply_text(f"Error consultando: {e}")

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 CÓMO USAR EL BOT\n\n"
        "1. El bot escanea cada 5 minutos\n"
        "2. Te alerta solo si spread es 3% o más\n"
        "3. Tocás el link y abre Binance directo\n"
        "4. Después registrás con:\n"
        "   /operacion 300 1000 1032\n\n"
        "Comandos:\n"
        "/estado — mercado ahora\n"
        "/resumen — mes actual\n"
        "/historial — últimas 10 ops\n"
        "/operacion — registrar trade"
    )

# ============================================================
# MAIN
# ============================================================
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    init_db()
    logging.info("Base de datos iniciada")

    # Scheduler en background
    scheduler = BackgroundScheduler()
    scheduler.add_job(escanear, "interval", minutes=INTERVALO_MINUTOS)
    scheduler.start()
    logging.info(f"Escáner iniciado cada {INTERVALO_MINUTOS} minutos")

    # Primer escaneo
    threading.Thread(target=escanear, daemon=True).start()

    # Bot Telegram (bloqueante)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("operacion", cmd_operacion))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    logging.info("Bot Telegram iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
