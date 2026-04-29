import os
import json
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.database import (
    init_db, get_lote_by_phone, save_pago, get_all_pagos,
    get_pagos_by_lote, get_projects, get_project, create_project,
    get_lotes_by_project, get_lote, update_lote_contract,
    add_lote_client, get_stats, update_pago, delete_pago,
    get_recibos_pendientes
)
from app.extractor import extract_spei_data, extract_contract_data, check_discrepancy
from app.drive import upload_to_drive
from app.whatsapp import send_message, download_media

load_dotenv()

app = FastAPI(title="PagoFlow v2")
app.mount("/static", StaticFiles(directory="static"), name="static")

VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "pagoflow_secret")
MY_PHONE     = os.getenv("MY_PHONE")


@app.on_event("startup")
async def startup():
    init_db()


# ─── Webhook WhatsApp ─────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    p = dict(request.query_params)
    if p.get("hub.verify_token") == VERIFY_TOKEN:
        return int(p.get("hub.challenge", 0))
    raise HTTPException(403)


@app.post("/webhook")
async def receive_message(request: Request, bg: BackgroundTasks):
    body = await request.json()
    try:
        msg      = body["entry"][0]["changes"][0]["value"]["messages"][0]
        phone    = msg["from"]
        msg_type = msg.get("type")
    except (KeyError, IndexError):
        return {"status": "ignored"}

    # Comando ASIGNAR del dueño
    if msg_type == "text" and phone == MY_PHONE:
        texto = msg.get("text", {}).get("body", "")
        if texto.upper().startswith("ASIGNAR"):
            bg.add_task(handle_asignar, texto)
            return {"status": "processing"}

    if msg_type not in ("image", "document"):
        return {"status": "not_media"}

    media_id = msg.get(msg_type, {}).get("id")
    if media_id:
        bg.add_task(process_comprobante, phone, media_id, msg_type)

    return {"status": "processing"}


@app.post("/webhook/manychat")
async def receive_manychat(request: Request, bg: BackgroundTasks):
    body = await request.json()
    print(f"📩 Manychat body: {json.dumps(body)}")
    phone     = body.get("phone", "")
    media_url = body.get("media_url", "")
    media_type = body.get("media_type", "image")

    if not phone or not media_url:
        print(f"⚠️ Ignorado — phone='{phone}' media_url='{media_url}'")
        return {"status": "ignored"}

    bg.add_task(process_comprobante_url, phone, media_url, media_type)
    return {"status": "processing"}


# ─── Procesamiento automático del comprobante ─────────────────────────────────

async def process_comprobante_url(phone: str, media_url: str, media_type: str):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(media_url)
        if resp.status_code != 200:
            return
    mime_type = "application/pdf" if media_type == "document" else "image/jpeg"
    await _process(phone, resp.content, mime_type)


async def process_comprobante(phone: str, media_id: str, media_type: str):
    image_bytes, mime_type = await download_media(media_id)
    if not image_bytes:
        return
    await _process(phone, image_bytes, mime_type)


async def _process(phone: str, image_bytes: bytes, mime_type: str):
    lote = get_lote_by_phone(phone)
    if not lote:
        await send_message(
            MY_PHONE,
            f"📋 *Número nuevo*\n+{phone}\n\n"
            f"Responde:\n`ASIGNAR {phone} LOTE-XX NOMBRE CLIENTE`"
        )
        return

    # Extraer datos del comprobante con IA
    spei = await extract_spei_data(image_bytes, mime_type)
    if not spei:
        await send_message(MY_PHONE, f"⚠️ No pude leer comprobante de {lote['cliente_nombre']}")
        return

    # Detectar discrepancias contra el contrato
    estatus, discrepancia = check_discrepancy(spei, lote)

    # Nombre del archivo: Pago_Lote07_MarioRamirez_28Abr2026_$12000.jpg
    fecha_str = spei.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    try:
        fecha_fmt = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d%b%Y")
    except Exception:
        fecha_fmt = fecha_str.replace("-", "")

    monto_str  = str(int(float(spei.get("monto") or 0)))
    nombre_fmt = lote["cliente_nombre"].replace(" ", "")
    lote_num   = str(lote["numero"]).zfill(2)
    ext        = "pdf" if "pdf" in mime_type else "jpg"
    filename   = f"Pago_Lote{lote_num}_{nombre_fmt}_{fecha_fmt}_${monto_str}.{ext}"

    # Subir a Drive en carpeta del proyecto/lote
    folder   = f"PagoFlow/Lote-{lote_num}/Comprobantes"
    drive_ok = await upload_to_drive(image_bytes, filename, folder, mime_type)

    # Guardar pago
    contrato_json = json.loads(lote["contrato_json"]) if lote.get("contrato_json") else {}
    monto_esperado = contrato_json.get("mensualidad", 0)

    save_pago(
        lote_id        = lote["id"],
        monto          = spei.get("monto"),
        monto_esperado = monto_esperado,
        referencia     = spei.get("referencia"),
        banco          = spei.get("banco"),
        fecha          = fecha_str,
        archivo_drive  = filename,
        drive_ok       = drive_ok,
        discrepancia   = discrepancia,
        estatus        = estatus,
    )

    # Alerta al dueño
    icono = "✅" if estatus == "ok" else "⚠️"
    msg_parts = [
        f"{icono} *Pago registrado*",
        f"👤 {lote['cliente_nombre']}",
        f"🏠 Lote {lote['numero']}",
        f"💰 ${spei.get('monto'):,.2f} MXN",
        f"🔢 {spei.get('referencia', '—')}",
        f"🏦 {spei.get('banco', '—')}",
        f"📁 Drive: {'✅' if drive_ok else '❌'}",
    ]
    if discrepancia:
        msg_parts.append(f"⚠️ *Discrepancia:* {discrepancia}")

    await send_message(MY_PHONE, "\n".join(msg_parts))


async def handle_asignar(texto: str):
    try:
        partes  = texto.split(maxsplit=4)
        phone   = partes[1]
        lote_n  = partes[2].replace("LOTE-", "").replace("lote-", "")
        nombre  = partes[3] if len(partes) > 3 else "Sin nombre"
        # Usar proyecto 1 por default
        add_lote_client(1, lote_n, nombre, phone)
        await send_message(MY_PHONE, f"✅ {nombre} → Lote {lote_n} registrado")
    except Exception as e:
        await send_message(MY_PHONE, f"⚠️ Error: {e}")


# ─── API proyectos ────────────────────────────────────────────────────────────

@app.get("/api/projects")
def api_projects():
    return get_projects()

@app.post("/api/projects")
async def api_create_project(request: Request):
    d = await request.json()
    pid = create_project(d["nombre"], d.get("descripcion", ""))
    return {"id": pid}

@app.get("/api/projects/{pid}/lotes")
def api_lotes(pid: int):
    lotes = get_lotes_by_project(pid)
    for l in lotes:
        if l.get("contrato_json"):
            l["contrato_json"] = json.loads(l["contrato_json"])
    return lotes

@app.get("/api/lotes/{lid}/pagos")
def api_pagos_lote(lid: int):
    return get_pagos_by_lote(lid)

@app.get("/api/lotes/{lid}")
def api_lote_detail(lid: int):
    l = get_lote(lid)
    if l and l.get("contrato_json"):
        l["contrato_json"] = json.loads(l["contrato_json"])
    return l

# ─── Subir comprobante manual ────────────────────────────────────────────────

@app.post("/api/lotes/{lid}/comprobante")
async def upload_comprobante_manual(lid: int, file: UploadFile = File(...)):
    lote = get_lote(lid)
    if not lote:
        raise HTTPException(404, "Lote no encontrado")

    file_bytes = await file.read()
    mime_type  = file.content_type or "image/jpeg"

    await _process(lote["cliente_phone"], file_bytes, mime_type)
    return {"status": "ok"}


# ─── Subir contrato PDF ───────────────────────────────────────────────────────

@app.post("/api/lotes/{lid}/contrato")
async def upload_contrato(lid: int, file: UploadFile = File(...)):
    file_bytes = await file.read()
    mime_type  = file.content_type or "application/pdf"

    contrato = await extract_contract_data(file_bytes, mime_type)
    if not contrato:
        raise HTTPException(400, "No se pudo leer el contrato")

    monto_total = float(contrato.get("monto_total") or 0)

    lote = get_lote(lid)
    lote_num   = str(lote["numero"]).zfill(2)
    nombre_fmt = lote["cliente_nombre"].replace(" ", "")
    ext        = "jpg" if mime_type.startswith("image/") else "pdf"
    filename   = f"Contrato_Lote{lote_num}_{nombre_fmt}.{ext}"
    folder     = f"PagoFlow/Lote-{lote_num}/Contratos"
    drive_ok = await upload_to_drive(file_bytes, filename, folder, mime_type)

    update_lote_contract(lid, contrato, filename, monto_total)

    # Solo el pago_a_la_firma se cobra al momento de firmar
    enganche = float(contrato.get("pago_a_la_firma") or contrato.get("enganche") or 0)
    if enganche > 0:
        pagos_lote = get_pagos_by_lote(lid)
        pago_enganche = next((p for p in pagos_lote if p.get("referencia") == "ENGANCHE"), None)
        if pago_enganche:
            # Contrato reemplazado — actualizar monto del enganche si cambió
            if float(pago_enganche.get("monto") or 0) != enganche:
                update_pago(pago_enganche["id"], enganche, pago_enganche["fecha"],
                            "ENGANCHE", "Firma de contrato")
        else:
            # Primera vez — registrar enganche
            save_pago(
                lote_id        = lid,
                monto          = enganche,
                monto_esperado = enganche,
                referencia     = "ENGANCHE",
                banco          = "Firma de contrato",
                fecha          = datetime.now().strftime("%Y-%m-%d"),
                archivo_drive  = filename,
                drive_ok       = drive_ok,
                discrepancia   = None,
                estatus        = "ok",
            )

    return {"status": "ok", "contrato": contrato}

# ─── Editar lote ─────────────────────────────────────────────────────────────

@app.put("/api/lotes/{lid}")
async def api_edit_lote(lid: int, request: Request):
    d = await request.json()
    from app.database import update_lote_info
    update_lote_info(lid, d["numero"], d["nombre"], d["phone"])
    return {"status": "ok"}


# ─── Editar / eliminar pago ──────────────────────────────────────────────────

@app.put("/api/pagos/{pid}")
async def api_edit_pago(pid: int, request: Request):
    d = await request.json()
    update_pago(pid, float(d["monto"]), d["fecha"], d.get("referencia",""), d.get("banco",""))
    return {"status": "ok"}

@app.delete("/api/pagos/{pid}")
async def api_delete_pago(pid: int):
    delete_pago(pid)
    return {"status": "ok"}

# ─── Agregar cliente/lote ─────────────────────────────────────────────────────

@app.post("/api/projects/{pid}/lotes")
async def api_add_lote(pid: int, request: Request):
    d = await request.json()
    add_lote_client(pid, d["numero"], d["nombre"], d["phone"])
    lote = get_lote_by_phone(d["phone"])
    return {"status": "ok", "id": lote["id"] if lote else None}

# ─── Generador de recibos ────────────────────────────────────────────────────

@app.post("/api/lotes/{lid}/recibo")
async def api_generar_recibo(lid: int, request: Request):
    from app.recibo import generar_recibo

    lote = get_lote(lid)
    if not lote:
        raise HTTPException(404, "Lote no encontrado")
    if not lote.get("contrato_json"):
        raise HTTPException(400, "El lote no tiene contrato cargado")

    d         = await request.json()
    monto     = float(d.get("monto") or 0)
    tipo      = d.get("tipo", "mensualidad")
    forma_pago = d.get("forma_pago", "transferencia")

    contrato  = json.loads(lote["contrato_json"]) if isinstance(lote["contrato_json"], str) else lote["contrato_json"]
    proyecto  = get_project(lote["project_id"])
    pagos     = get_pagos_by_lote(lid)

    mens_pagadas    = sum(1 for p in pagos if p.get("referencia") != "ENGANCHE")
    num_mensualidad = mens_pagadas + 1

    vendedor_nombre = os.getenv("VENDEDOR_NOMBRE", "Vendedor")
    ciudad          = os.getenv("CIUDAD", "Monterrey")
    estado          = os.getenv("ESTADO", "Nuevo León")

    doc_bytes = generar_recibo(
        lote            = lote,
        proyecto        = proyecto,
        contrato        = contrato,
        num_mensualidad = num_mensualidad,
        monto           = monto,
        tipo            = tipo,
        forma_pago      = forma_pago,
        vendedor_nombre = vendedor_nombre,
        ciudad          = ciudad,
        estado          = estado,
    )

    lote_num = str(lote["numero"]).zfill(2)
    nombre_f = lote["cliente_nombre"].replace(" ", "")
    filename = f"Recibo_Lote{lote_num}_{nombre_f}_Mens{num_mensualidad}.docx"

    return Response(
        content    = doc_bytes,
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers    = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ─── Stats y pagos globales ───────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    return get_stats()

@app.get("/api/recibos-pendientes")
def api_recibos_pendientes():
    return get_recibos_pendientes()

@app.get("/api/pagos")
def api_all_pagos():
    return get_all_pagos()

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("static/index.html") as f:
        return f.read()
