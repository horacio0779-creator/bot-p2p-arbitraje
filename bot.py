 import requests
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURACIÓN
# ============================================================
TELEGRAM_TOKEN = "8498685576:AAGg7EKaW1UkOZRdUdArYAtzZfieovgNljI"
CHAT_ID = "1112728778"
CAPITAL_USDT = 300          # Capital operativo en USDT
SPREAD_MINIMO = 3.0         # % mínimo para alertar
REP_MINIMA = 92             # % reputación mínima
OPS_MINIMAS = 100           # Operaciones mínimas del anunciante
INTERVALO_MINUTOS = 5       # Cada cuántos minutos escanea
META_MENSUAL_USD = 117      # Meta mensual en USD

# ============================================================
# BASE DE DATOS
# ============================================================
def init_db():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    # Historial de precios
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
    
    # Operaciones registradas
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
        "payTypes": ["Mercadopago"],
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

def filtrar_ofertas(ofertas, trade_type):
    """Filtra por reputación, operaciones y monto compatible"""
    resultado = []
    capital_ars_estimado = CAPITAL_USDT * 1100  # estimación base

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
            
            # Calcular monto en ARS para este precio
            monto_ars = CAPITAL_USDT * precio

            # Aplicar filtros
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
    gasto_ars = monto_usdt * precio_compra
    recibo_ars = monto_usdt * precio_venta
    ganancia_ars = recibo_ars - gasto_ars
    # Tipo de cambio aproximado para convertir a USD
    usd_rate = precio_venta  # 1 USDT ≈ precio_venta ARS
    ganancia_usd = ganancia_ars / usd_rate
    return ganancia_ars, ganancia_usd

# ============================================================
# ANÁLISIS HORARIO
# ============================================================
def get_stats_horarias():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    # Spread promedio por hora (últimos 7 días)
    c.execute("""
        SELECT hora, AVG(spread), COUNT(*) 
        FROM precios 
        WHERE timestamp > datetime('now', '-7 days')
        AND spread > 0
        GROUP BY hora 
        ORDER BY AVG(spread) DESC
    """)
    stats = c.fetchall()
    conn.close()
    return stats

def get_mejor_hora():
    stats = get_stats_horarias()
    if not stats:
        return None, None
    mejor = stats[0]
    return mejor[0], mejor[1]

def get_stats_generales():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    hora_actual = datetime.now().hour
    
    # Spread promedio última hora
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-1 hour') AND spread > 0""")
    avg_1h = c.fetchone()[0] or 0

    # Spread promedio 24hs
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-24 hours') AND spread > 0""")
    avg_24h = c.fetchone()[0] or 0

    # Spread promedio 7 días
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-7 days') AND spread > 0""")
    avg_7d = c.fetchone()[0] or 0

    # Spread promedio de esta hora históricamente
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE hora = ? AND spread > 0""", (hora_actual,))
    avg_esta_hora = c.fetchone()[0] or 0

    conn.close()
    return avg_1h, avg_24h, avg_7d, avg_esta_hora

def clasificar_momento(spread_actual, avg_esta_hora, mejor_hora, hora_actual):
    if avg_esta_hora == 0:
        return "📊 Sin datos históricos aún"
    
    ratio = spread_actual / avg_esta_hora if avg_esta_hora > 0 else 1
    
    if hora_actual == mejor_hora and ratio >= 1.2:
        return "🔥 MOMENTO ÓPTIMO"
    elif ratio >= 1.1:
        return "⭐ Buen momento"
    elif ratio >= 0.9:
        return "✅ Momento normal"
    else:
        return "⚠️ Momento bajo"

# ============================================================
# RESUMEN MENSUAL
# ============================================================
def get_resumen_mes():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    mes_inicio = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    
    c.execute("""
        SELECT COUNT(*), SUM(ganancia_ars), SUM(ganancia_usd), 
               MAX(spread), AVG(spread)
        FROM operaciones 
        WHERE timestamp >= ?
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
# ENVIAR ALERTA
# ============================================================
def enviar_alerta(vendedor, comprador, spread, ganancia_ars, ganancia_usd):
    bot = Bot(token=TELEGRAM_TOKEN)
    
    avg_1h, avg_24h, avg_7d, avg_esta_hora = get_stats_generales()
    mejor_hora, mejor_spread_hora = get_mejor_hora()
    hora_actual = datetime.now().hour
    momento = clasificar_momento(spread, avg_esta_hora, mejor_hora, hora_actual)
    
    resumen = get_resumen_mes()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)
    
    msg = f"""🟢 *OPORTUNIDAD DE ARBITRAJE*
📅 {datetime.now().strftime("%d/%m/%Y %H:%M")}hs

*PASO 1 — COMPRÁ primero:*
👤 {vendedor['nickname']}
⭐ {vendedor['reputacion']:.1f}% ({vendedor['operaciones']} ops)
💰 ${vendedor['precio']:,.2f} ARS/USDT
📊 Límite: ${vendedor['min_ars']:,.0f} – ${vendedor['max_ars']:,.0f} ARS
🔗 [Abrir anunciante en Binance]({vendedor['link']})

*PASO 2 — VENDÉ inmediatamente:*
👤 {comprador['nickname']}
⭐ {comprador['reputacion']:.1f}% ({comprador['operaciones']} ops)
💰 ${comprador['precio']:,.2f} ARS/USDT
📊 Límite: ${comprador['min_ars']:,.0f} – ${comprador['max_ars']:,.0f} ARS
🔗 [Abrir anunciante en Binance]({comprador['link']})

💵 *Capital:* {CAPITAL_USDT} USDT
📈 *Spread neto:* {spread:.2f}%
🏦 *Ganancia estimada:* ${ganancia_ars:,.0f} ARS (~${ganancia_usd:.1f} USD)

📊 *Estadística del momento:*
   • Spread última hora: {avg_1h:.2f}%
   • Spread 24hs: {avg_24h:.2f}%
   • Spread 7 días: {avg_7d:.2f}%
   • Este spread vs hora histórica: {((spread/avg_esta_hora-1)*100):.0f}% {"🔥" if spread > avg_esta_hora else ""}
   • Mejor hora del día: {mejor_hora:02d}:00hs ({mejor_spread_hora:.2f}% avg)
   • {momento}

🎯 *Meta mensual:*
   Objetivo: ${META_MENSUAL_USD} USD
   Acumulado: ${resumen['ganancia_usd']:.1f} USD
   {barra_str} {progreso:.0f}%

⏳ _Actuá rápido — ventana de 5 a 10 min_
📝 _Registrá con /operacion monto compra venta_"""

    import asyncio
    asyncio.run(bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=False
    ))

# ============================================================
# ESCANEO PRINCIPAL
# ============================================================
def escanear():
    logging.info(f"Escaneando... {datetime.now().strftime('%H:%M:%S')}")
    
    ofertas_venta = get_ofertas("BUY")   # Yo compro → busco quien vende
    ofertas_compra = get_ofertas("SELL") # Yo vendo → busco quien compra
    
    vendedores = filtrar_ofertas(ofertas_venta, "BUY")
    compradores = filtrar_ofertas(ofertas_compra, "SELL")
    
    if not vendedores or not compradores:
        logging.info("Sin ofertas válidas en este momento")
        return
    
    # Mejor oportunidad: comprar más barato, vender más caro
    mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
    mejor_comprador = max(compradores, key=lambda x: x["precio"])
    
    spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
    ganancia_ars, ganancia_usd = calcular_ganancia(
        CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"]
    )
    
    # Guardar en base de datos
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO precios (timestamp, hora, mejor_venta, mejor_compra, spread, 
                            anunciantes_venta, anunciantes_compra)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        datetime.now().hour,
        mejor_vendedor["precio"],
        mejor_comprador["precio"],
        max(spread, 0),
        len(vendedores),
        len(compradores)
    ))
    conn.commit()
    conn.close()
    
    logging.info(f"Spread actual: {spread:.2f}% | Mínimo requerido: {SPREAD_MINIMO}%")
    
    # Alertar solo si spread supera mínimo
    if spread >= SPREAD_MINIMO:
        logging.info(f"¡Oportunidad! Spread: {spread:.2f}%")
        enviar_alerta(mejor_vendedor, mejor_comprador, spread, ganancia_ars, ganancia_usd)

# ============================================================
# COMANDOS TELEGRAM
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot P2P Arbitraje ARS/USDT activo*\n\n"
        "Comandos disponibles:\n"
        "/operacion — registrar operación\n"
        "/resumen — ver estadísticas del mes\n"
        "/historial — últimas operaciones\n"
        "/estado — estado actual del mercado\n"
        "/ayuda — cómo usar el bot",
        parse_mode="Markdown"
    )

async def cmd_operacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "❌ Formato: /operacion MONTO PRECIO\\_COMPRA PRECIO\\_VENTA\n"
                "Ejemplo: /operacion 300 1000 1032"
            )
            return
        
        monto = float(args[0])
        precio_compra = float(args[1])
        precio_venta = float(args[2])
        
        spread = calcular_spread(precio_compra, precio_venta)
        ganancia_ars, ganancia_usd = calcular_ganancia(monto, precio_compra, precio_venta)
        
        # Guardar
        conn = sqlite3.connect("p2p_data.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO operaciones (timestamp, monto_usdt, precio_compra, precio_venta,
                                    spread, ganancia_ars, ganancia_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), monto, precio_compra, precio_venta,
              spread, ganancia_ars, ganancia_usd))
        conn.commit()
        conn.close()
        
        resumen = get_resumen_mes()
        progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
        barras = int(progreso / 10)
        barra_str = "█" * barras + "░" * (10 - barras)
        faltante = max(0, META_MENSUAL_USD - resumen["ganancia_usd"])
        
        await update.message.reply_text(
            f"✅ *OPERACIÓN REGISTRADA*\n\n"
            f"📊 *Esta operación:*\n"
            f"   Compraste: ${monto * precio_compra:,.0f} ARS\n"
            f"   Vendiste: ${monto * precio_venta:,.0f} ARS\n"
            f"   Spread: {spread:.2f}%\n"
            f"   Ganancia bruta: ${ganancia_ars:,.0f} ARS\n"
            f"   Ganancia neta: ~${ganancia_usd:.2f} USD\n\n"
            f"📈 *Resumen del mes:*\n"
            f"   Operaciones: {resumen['total_ops']}\n"
            f"   Ganancia total: ${resumen['ganancia_usd']:.2f} USD\n"
            f"   Mejor spread: {resumen['mejor_spread']:.2f}%\n"
            f"   Spread promedio: {resumen['spread_promedio']:.2f}%\n\n"
            f"🎯 *Meta mensual:*\n"
            f"   Objetivo: ${META_MENSUAL_USD} USD\n"
            f"   Acumulado: ${resumen['ganancia_usd']:.2f} USD\n"
            f"   Faltante: ${faltante:.2f} USD\n"
            f"   {barra_str} {progreso:.0f}%",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resumen = get_resumen_mes()
    stats = get_stats_horarias()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)
    
    mejores_horas = ""
    for i, (hora, avg, count) in enumerate(stats[:3]):
        mejores_horas += f"   {i+1}. {hora:02d}:00hs → {avg:.2f}% ({count} muestras)\n"
    
    await update.message.reply_text(
        f"📊 *RESUMEN DEL MES*\n\n"
        f"💼 Operaciones: {resumen['total_ops']}\n"
        f"💵 Ganancia total: ${resumen['ganancia_usd']:.2f} USD\n"
        f"💰 En pesos: ${resumen['ganancia_ars']:,.0f} ARS\n"
        f"📈 Mejor spread: {resumen['mejor_spread']:.2f}%\n"
        f"📊 Spread promedio: {resumen['spread_promedio']:.2f}%\n\n"
        f"🕐 *Mejores horarios (histórico):*\n{mejores_horas}\n"
        f"🎯 *Meta: ${META_MENSUAL_USD} USD*\n"
        f"   {barra_str} {progreso:.0f}%\n"
        f"   Acumulado: ${resumen['ganancia_usd']:.2f} / ${META_MENSUAL_USD} USD",
        parse_mode="Markdown"
    )

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, monto_usdt, spread, ganancia_usd 
        FROM operaciones 
        ORDER BY timestamp DESC LIMIT 10
    """)
    ops = c.fetchall()
    conn.close()
    
    if not ops:
        await update.message.reply_text("📭 No hay operaciones registradas aún.")
        return
    
    texto = "📋 *ÚLTIMAS OPERACIONES*\n\n"
    for op in ops:
        fecha = datetime.fromisoformat(op[0]).strftime("%d/%m %H:%M")
        texto += f"• {fecha} | {op[1]:.0f} USDT | {op[2]:.1f}% | +${op[3]:.2f} USD\n"
    
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Consultando mercado...")
    
    ofertas_venta = get_ofertas("BUY")
    ofertas_compra = get_ofertas("SELL")
    vendedores = filtrar_ofertas(ofertas_venta, "BUY")
    compradores = filtrar_ofertas(ofertas_compra, "SELL")
    
    if not vendedores or not compradores:
        await update.message.reply_text("⚠️ Sin ofertas válidas en este momento con los filtros actuales.")
        return
    
    mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
    mejor_comprador = max(compradores, key=lambda x: x["precio"])
    spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
    _, ganancia_usd = calcular_ganancia(CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"])
    
    avg_1h, avg_24h, avg_7d, _ = get_stats_generales()
    
    estado = "🟢 OPORTUNIDAD" if spread >= SPREAD_MINIMO else "🔴 Sin oportunidad"
    
    await update.message.reply_text(
        f"📡 *ESTADO DEL MERCADO AHORA*\n\n"
        f"{estado}\n\n"
        f"Mejor precio compra: ${mejor_vendedor['precio']:,.2f} ARS\n"
        f"Mejor precio venta: ${mejor_comprador['precio']:,.2f} ARS\n"
        f"Spread actual: {spread:.2f}%\n"
        f"Ganancia estimada: ${ganancia_usd:.2f} USD\n\n"
        f"📊 Promedios:\n"
        f"   Última hora: {avg_1h:.2f}%\n"
        f"   24 horas: {avg_24h:.2f}%\n"
        f"   7 días: {avg_7d:.2f}%\n\n"
        f"Vendedores válidos: {len(vendedores)}\n"
        f"Compradores válidos: {len(compradores)}",
        parse_mode="Markdown"
    )

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *CÓMO USAR EL BOT*\n\n"
        "1️⃣ El bot escanea automáticamente cada 5 minutos\n"
        "2️⃣ Cuando encuentra spread ≥ 3% te manda alerta\n"
        "3️⃣ Tocás el link → abre Binance → operás\n"
        "4️⃣ Después registrás con:\n"
        "   `/operacion 300 1000 1032`\n"
        "   (monto, precio compra, precio venta)\n\n"
        "📌 *Comandos:*\n"
        "/estado — ver mercado ahora mismo\n"
        "/resumen — estadísticas del mes\n"
        "/historial — últimas 10 operaciones\n"
        "/operacion — registrar operación",
        parse_mode="Markdown"
    )

# ============================================================
# MAIN
# ============================================================
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    init_db()
    logging.info("Base de datos iniciada")
    
    # Scheduler para escaneo automático
    scheduler = BlockingScheduler()
    
    # Telegram bot en hilo separado
    import threading
    
    def run_telegram():
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("operacion", cmd_operacion))
        app.add_handler(CommandHandler("resumen", cmd_resumen))
        app.add_handler(CommandHandler("historial", cmd_historial))
        app.add_handler(CommandHandler("estado", cmd_estado))
        app.add_handler(CommandHandler("ayuda", cmd_ayuda))
        logging.info("Bot Telegram iniciado")
        app.run_polling()
    
    telegram_thread = threading.Thread(target=run_telegram, daemon=True)
    telegram_thread.start()
    
    # Primer escaneo inmediato
    escanear()
    
    # Escaneo cada X minutos
    scheduler.add_job(escanear, "interval", minutes=INTERVALO_MINUTOS)
    logging.info(f"Escáner iniciado cada {INTERVALO_MINUTOS} minutos")
    scheduler.start()

if __name__ == "__main__":
    main()
           spread REAL,
            anunciantes_venta INTEGER,
            anunciantes_compra INTEGER
        )
    """)
    
    # Operaciones registradas
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
        "payTypes": ["Mercadopago"],
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

def filtrar_ofertas(ofertas, trade_type):
    """Filtra por reputación, operaciones y monto compatible"""
    resultado = []
    capital_ars_estimado = CAPITAL_USDT * 1100  # estimación base

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
            
            # Calcular monto en ARS para este precio
            monto_ars = CAPITAL_USDT * precio

            # Aplicar filtros
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
    gasto_ars = monto_usdt * precio_compra
    recibo_ars = monto_usdt * precio_venta
    ganancia_ars = recibo_ars - gasto_ars
    # Tipo de cambio aproximado para convertir a USD
    usd_rate = precio_venta  # 1 USDT ≈ precio_venta ARS
    ganancia_usd = ganancia_ars / usd_rate
    return ganancia_ars, ganancia_usd

# ============================================================
# ANÁLISIS HORARIO
# ============================================================
def get_stats_horarias():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    # Spread promedio por hora (últimos 7 días)
    c.execute("""
        SELECT hora, AVG(spread), COUNT(*) 
        FROM precios 
        WHERE timestamp > datetime('now', '-7 days')
        AND spread > 0
        GROUP BY hora 
        ORDER BY AVG(spread) DESC
    """)
    stats = c.fetchall()
    conn.close()
    return stats

def get_mejor_hora():
    stats = get_stats_horarias()
    if not stats:
        return None, None
    mejor = stats[0]
    return mejor[0], mejor[1]

def get_stats_generales():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    hora_actual = datetime.now().hour
    
    # Spread promedio última hora
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-1 hour') AND spread > 0""")
    avg_1h = c.fetchone()[0] or 0

    # Spread promedio 24hs
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-24 hours') AND spread > 0""")
    avg_24h = c.fetchone()[0] or 0

    # Spread promedio 7 días
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE timestamp > datetime('now', '-7 days') AND spread > 0""")
    avg_7d = c.fetchone()[0] or 0

    # Spread promedio de esta hora históricamente
    c.execute("""SELECT AVG(spread) FROM precios 
                 WHERE hora = ? AND spread > 0""", (hora_actual,))
    avg_esta_hora = c.fetchone()[0] or 0

    conn.close()
    return avg_1h, avg_24h, avg_7d, avg_esta_hora

def clasificar_momento(spread_actual, avg_esta_hora, mejor_hora, hora_actual):
    if avg_esta_hora == 0:
        return "📊 Sin datos históricos aún"
    
    ratio = spread_actual / avg_esta_hora if avg_esta_hora > 0 else 1
    
    if hora_actual == mejor_hora and ratio >= 1.2:
        return "🔥 MOMENTO ÓPTIMO"
    elif ratio >= 1.1:
        return "⭐ Buen momento"
    elif ratio >= 0.9:
        return "✅ Momento normal"
    else:
        return "⚠️ Momento bajo"

# ============================================================
# RESUMEN MENSUAL
# ============================================================
def get_resumen_mes():
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    
    mes_inicio = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    
    c.execute("""
        SELECT COUNT(*), SUM(ganancia_ars), SUM(ganancia_usd), 
               MAX(spread), AVG(spread)
        FROM operaciones 
        WHERE timestamp >= ?
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
# ENVIAR ALERTA
# ============================================================
def enviar_alerta(vendedor, comprador, spread, ganancia_ars, ganancia_usd):
    bot = Bot(token=TELEGRAM_TOKEN)
    
    avg_1h, avg_24h, avg_7d, avg_esta_hora = get_stats_generales()
    mejor_hora, mejor_spread_hora = get_mejor_hora()
    hora_actual = datetime.now().hour
    momento = clasificar_momento(spread, avg_esta_hora, mejor_hora, hora_actual)
    
    resumen = get_resumen_mes()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)
    
    msg = f"""🟢 *OPORTUNIDAD DE ARBITRAJE*
📅 {datetime.now().strftime("%d/%m/%Y %H:%M")}hs

*PASO 1 — COMPRÁ primero:*
👤 {vendedor['nickname']}
⭐ {vendedor['reputacion']:.1f}% ({vendedor['operaciones']} ops)
💰 ${vendedor['precio']:,.2f} ARS/USDT
📊 Límite: ${vendedor['min_ars']:,.0f} – ${vendedor['max_ars']:,.0f} ARS
🔗 [Abrir anunciante en Binance]({vendedor['link']})

*PASO 2 — VENDÉ inmediatamente:*
👤 {comprador['nickname']}
⭐ {comprador['reputacion']:.1f}% ({comprador['operaciones']} ops)
💰 ${comprador['precio']:,.2f} ARS/USDT
📊 Límite: ${comprador['min_ars']:,.0f} – ${comprador['max_ars']:,.0f} ARS
🔗 [Abrir anunciante en Binance]({comprador['link']})

💵 *Capital:* {CAPITAL_USDT} USDT
📈 *Spread neto:* {spread:.2f}%
🏦 *Ganancia estimada:* ${ganancia_ars:,.0f} ARS (~${ganancia_usd:.1f} USD)

📊 *Estadística del momento:*
   • Spread última hora: {avg_1h:.2f}%
   • Spread 24hs: {avg_24h:.2f}%
   • Spread 7 días: {avg_7d:.2f}%
   • Este spread vs hora histórica: {((spread/avg_esta_hora-1)*100):.0f}% {"🔥" if spread > avg_esta_hora else ""}
   • Mejor hora del día: {mejor_hora:02d}:00hs ({mejor_spread_hora:.2f}% avg)
   • {momento}

🎯 *Meta mensual:*
   Objetivo: ${META_MENSUAL_USD} USD
   Acumulado: ${resumen['ganancia_usd']:.1f} USD
   {barra_str} {progreso:.0f}%

⏳ _Actuá rápido — ventana de 5 a 10 min_
📝 _Registrá con /operacion monto compra venta_"""

    import asyncio
    asyncio.run(bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=False
    ))

# ============================================================
# ESCANEO PRINCIPAL
# ============================================================
def escanear():
    logging.info(f"Escaneando... {datetime.now().strftime('%H:%M:%S')}")
    
    ofertas_venta = get_ofertas("BUY")   # Yo compro → busco quien vende
    ofertas_compra = get_ofertas("SELL") # Yo vendo → busco quien compra
    
    vendedores = filtrar_ofertas(ofertas_venta, "BUY")
    compradores = filtrar_ofertas(ofertas_compra, "SELL")
    
    if not vendedores or not compradores:
        logging.info("Sin ofertas válidas en este momento")
        return
    
    # Mejor oportunidad: comprar más barato, vender más caro
    mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
    mejor_comprador = max(compradores, key=lambda x: x["precio"])
    
    spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
    ganancia_ars, ganancia_usd = calcular_ganancia(
        CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"]
    )
    
    # Guardar en base de datos
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO precios (timestamp, hora, mejor_venta, mejor_compra, spread, 
                            anunciantes_venta, anunciantes_compra)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        datetime.now().hour,
        mejor_vendedor["precio"],
        mejor_comprador["precio"],
        max(spread, 0),
        len(vendedores),
        len(compradores)
    ))
    conn.commit()
    conn.close()
    
    logging.info(f"Spread actual: {spread:.2f}% | Mínimo requerido: {SPREAD_MINIMO}%")
    
    # Alertar solo si spread supera mínimo
    if spread >= SPREAD_MINIMO:
        logging.info(f"¡Oportunidad! Spread: {spread:.2f}%")
        enviar_alerta(mejor_vendedor, mejor_comprador, spread, ganancia_ars, ganancia_usd)

# ============================================================
# COMANDOS TELEGRAM
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot P2P Arbitraje ARS/USDT activo*\n\n"
        "Comandos disponibles:\n"
        "/operacion — registrar operación\n"
        "/resumen — ver estadísticas del mes\n"
        "/historial — últimas operaciones\n"
        "/estado — estado actual del mercado\n"
        "/ayuda — cómo usar el bot",
        parse_mode="Markdown"
    )

async def cmd_operacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "❌ Formato: /operacion MONTO PRECIO\\_COMPRA PRECIO\\_VENTA\n"
                "Ejemplo: /operacion 300 1000 1032"
            )
            return
        
        monto = float(args[0])
        precio_compra = float(args[1])
        precio_venta = float(args[2])
        
        spread = calcular_spread(precio_compra, precio_venta)
        ganancia_ars, ganancia_usd = calcular_ganancia(monto, precio_compra, precio_venta)
        
        # Guardar
        conn = sqlite3.connect("p2p_data.db")
        c = conn.cursor()
        c.execute("""
            INSERT INTO operaciones (timestamp, monto_usdt, precio_compra, precio_venta,
                                    spread, ganancia_ars, ganancia_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), monto, precio_compra, precio_venta,
              spread, ganancia_ars, ganancia_usd))
        conn.commit()
        conn.close()
        
        resumen = get_resumen_mes()
        progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
        barras = int(progreso / 10)
        barra_str = "█" * barras + "░" * (10 - barras)
        faltante = max(0, META_MENSUAL_USD - resumen["ganancia_usd"])
        
        await update.message.reply_text(
            f"✅ *OPERACIÓN REGISTRADA*\n\n"
            f"📊 *Esta operación:*\n"
            f"   Compraste: ${monto * precio_compra:,.0f} ARS\n"
            f"   Vendiste: ${monto * precio_venta:,.0f} ARS\n"
            f"   Spread: {spread:.2f}%\n"
            f"   Ganancia bruta: ${ganancia_ars:,.0f} ARS\n"
            f"   Ganancia neta: ~${ganancia_usd:.2f} USD\n\n"
            f"📈 *Resumen del mes:*\n"
            f"   Operaciones: {resumen['total_ops']}\n"
            f"   Ganancia total: ${resumen['ganancia_usd']:.2f} USD\n"
            f"   Mejor spread: {resumen['mejor_spread']:.2f}%\n"
            f"   Spread promedio: {resumen['spread_promedio']:.2f}%\n\n"
            f"🎯 *Meta mensual:*\n"
            f"   Objetivo: ${META_MENSUAL_USD} USD\n"
            f"   Acumulado: ${resumen['ganancia_usd']:.2f} USD\n"
            f"   Faltante: ${faltante:.2f} USD\n"
            f"   {barra_str} {progreso:.0f}%",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    resumen = get_resumen_mes()
    stats = get_stats_horarias()
    progreso = min(100, (resumen["ganancia_usd"] / META_MENSUAL_USD) * 100)
    barras = int(progreso / 10)
    barra_str = "█" * barras + "░" * (10 - barras)
    
    mejores_horas = ""
    for i, (hora, avg, count) in enumerate(stats[:3]):
        mejores_horas += f"   {i+1}. {hora:02d}:00hs → {avg:.2f}% ({count} muestras)\n"
    
    await update.message.reply_text(
        f"📊 *RESUMEN DEL MES*\n\n"
        f"💼 Operaciones: {resumen['total_ops']}\n"
        f"💵 Ganancia total: ${resumen['ganancia_usd']:.2f} USD\n"
        f"💰 En pesos: ${resumen['ganancia_ars']:,.0f} ARS\n"
        f"📈 Mejor spread: {resumen['mejor_spread']:.2f}%\n"
        f"📊 Spread promedio: {resumen['spread_promedio']:.2f}%\n\n"
        f"🕐 *Mejores horarios (histórico):*\n{mejores_horas}\n"
        f"🎯 *Meta: ${META_MENSUAL_USD} USD*\n"
        f"   {barra_str} {progreso:.0f}%\n"
        f"   Acumulado: ${resumen['ganancia_usd']:.2f} / ${META_MENSUAL_USD} USD",
        parse_mode="Markdown"
    )

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("p2p_data.db")
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, monto_usdt, spread, ganancia_usd 
        FROM operaciones 
        ORDER BY timestamp DESC LIMIT 10
    """)
    ops = c.fetchall()
    conn.close()
    
    if not ops:
        await update.message.reply_text("📭 No hay operaciones registradas aún.")
        return
    
    texto = "📋 *ÚLTIMAS OPERACIONES*\n\n"
    for op in ops:
        fecha = datetime.fromisoformat(op[0]).strftime("%d/%m %H:%M")
        texto += f"• {fecha} | {op[1]:.0f} USDT | {op[2]:.1f}% | +${op[3]:.2f} USD\n"
    
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Consultando mercado...")
    
    ofertas_venta = get_ofertas("BUY")
    ofertas_compra = get_ofertas("SELL")
    vendedores = filtrar_ofertas(ofertas_venta, "BUY")
    compradores = filtrar_ofertas(ofertas_compra, "SELL")
    
    if not vendedores or not compradores:
        await update.message.reply_text("⚠️ Sin ofertas válidas en este momento con los filtros actuales.")
        return
    
    mejor_vendedor = min(vendedores, key=lambda x: x["precio"])
    mejor_comprador = max(compradores, key=lambda x: x["precio"])
    spread = calcular_spread(mejor_vendedor["precio"], mejor_comprador["precio"])
    _, ganancia_usd = calcular_ganancia(CAPITAL_USDT, mejor_vendedor["precio"], mejor_comprador["precio"])
    
    avg_1h, avg_24h, avg_7d, _ = get_stats_generales()
    
    estado = "🟢 OPORTUNIDAD" if spread >= SPREAD_MINIMO else "🔴 Sin oportunidad"
    
    await update.message.reply_text(
        f"📡 *ESTADO DEL MERCADO AHORA*\n\n"
        f"{estado}\n\n"
        f"Mejor precio compra: ${mejor_vendedor['precio']:,.2f} ARS\n"
        f"Mejor precio venta: ${mejor_comprador['precio']:,.2f} ARS\n"
        f"Spread actual: {spread:.2f}%\n"
        f"Ganancia estimada: ${ganancia_usd:.2f} USD\n\n"
        f"📊 Promedios:\n"
        f"   Última hora: {avg_1h:.2f}%\n"
        f"   24 horas: {avg_24h:.2f}%\n"
        f"   7 días: {avg_7d:.2f}%\n\n"
        f"Vendedores válidos: {len(vendedores)}\n"
        f"Compradores válidos: {len(compradores)}",
        parse_mode="Markdown"
    )

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *CÓMO USAR EL BOT*\n\n"
        "1️⃣ El bot escanea automáticamente cada 5 minutos\n"
        "2️⃣ Cuando encuentra spread ≥ 3% te manda alerta\n"
        "3️⃣ Tocás el link → abre Binance → operás\n"
        "4️⃣ Después registrás con:\n"
        "   `/operacion 300 1000 1032`\n"
        "   (monto, precio compra, precio venta)\n\n"
        "📌 *Comandos:*\n"
        "/estado — ver mercado ahora mismo\n"
        "/resumen — estadísticas del mes\n"
        "/historial — últimas 10 operaciones\n"
        "/operacion — registrar operación",
        parse_mode="Markdown"
    )

# ============================================================
# MAIN
# ============================================================
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    init_db()
    logging.info("Base de datos iniciada")
    
    # Scheduler para escaneo automático
    scheduler = BlockingScheduler()
    
    # Telegram bot en hilo separado
    import threading
    
    def run_telegram():
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("operacion", cmd_operacion))
        app.add_handler(CommandHandler("resumen", cmd_resumen))
        app.add_handler(CommandHandler("historial", cmd_historial))
        app.add_handler(CommandHandler("estado", cmd_estado))
        app.add_handler(CommandHandler("ayuda", cmd_ayuda))
        logging.info("Bot Telegram iniciado")
        app.run_polling()
    
    telegram_thread = threading.Thread(target=run_telegram, daemon=True)
    telegram_thread.start()
    
    # Primer escaneo inmediato
    escanear()
    
    # Escaneo cada X minutos
    scheduler.add_job(escanear, "interval", minutes=INTERVALO_MINUTOS)
    logging.info(f"Escáner iniciado cada {INTERVALO_MINUTOS} minutos")
    scheduler.start()

if __name__ == "__main__":
    main()
