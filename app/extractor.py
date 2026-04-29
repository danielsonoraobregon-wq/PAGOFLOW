import os
import base64
import json
import httpx
from typing import Optional

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


async def extract_contract_data(file_bytes: bytes, mime_type: str = "application/pdf") -> Optional[dict]:
    """
    Lee el contrato (PDF o imagen) y extrae la información de pagos.
    """
    file_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    prompt = """Eres un extractor de contratos inmobiliarios mexicanos.

Lee este contrato y extrae ÚNICAMENTE la información relacionada con pagos y montos.
Devuelve SOLO este JSON sin texto adicional:

{
  "monto_total": 0.00,
  "enganche": 0.00,
  "mensualidad": 0.00,
  "total_mensualidades": 0,
  "fecha_inicio_pagos": "YYYY-MM-DD o null",
  "dia_pago": "día del mes en que vence cada pago, ej: 5",
  "penalizacion_mora": "descripción de penalización si aplica o null",
  "tabla_pagos": [
    {
      "numero": 1,
      "fecha_vencimiento": "YYYY-MM-DD",
      "monto_esperado": 0.00,
      "concepto": "Mensualidad 1"
    }
  ],
  "notas": "cualquier condición especial de pago mencionada en el contrato"
}

Reglas:
- Si la tabla de pagos tiene más de 60 registros, incluye solo los primeros 12 y el último
- Todos los montos en número sin símbolos ni comas
- Si no encuentras algún campo, pon null
- Responde SOLO con el JSON"""

    is_pdf = "pdf" in mime_type
    if is_pdf:
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64},
        }
    else:
        img_mime = mime_type if mime_type.startswith("image/") else "image/jpeg"
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": img_mime, "data": file_b64},
        }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}],
                },
            )

        if response.status_code != 200:
            print(f"Error Anthropic API contrato: {response.status_code} {response.text}")
            return None

        raw = response.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

    except Exception as e:
        print(f"Error leyendo contrato: {e}")
        return None


async def extract_spei_data(image_bytes: bytes, mime_type: str) -> Optional[dict]:
    """
    Lee imagen del comprobante SPEI y extrae los datos.
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    media_type = mime_type if mime_type.startswith("image/") else "image/jpeg"

    prompt = """Eres un extractor de comprobantes bancarios mexicanos (SPEI).

Extrae los datos de este comprobante y devuelve SOLO este JSON:
{
  "monto": 0.00,
  "referencia": "clave de rastreo completa",
  "banco": "banco emisor",
  "banco_destino": "banco receptor",
  "fecha": "YYYY-MM-DD",
  "hora": "HH:MM",
  "concepto": "concepto del pago o null",
  "nombre_emisor": "nombre del que envió o null"
}

- Monto en número sin símbolos
- Si un campo no aparece pon null
- Responde SOLO con el JSON"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": image_b64,
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                },
            )

        raw = response.json()["content"][0]["text"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

    except Exception as e:
        print(f"Error leyendo comprobante: {e}")
        return None


def check_discrepancy(spei_data: dict, lote: dict) -> tuple[str, str]:
    """
    Compara el pago recibido contra lo que dice el contrato.
    Devuelve (estatus, descripcion_discrepancia)
    estatus: 'ok' | 'monto_incorrecto' | 'sin_contrato' | 'adelantado'
    """
    contrato_json = lote.get("contrato_json")
    if not contrato_json:
        return "sin_contrato", "Lote sin contrato cargado"

    import json as _json
    try:
        contrato = _json.loads(contrato_json) if isinstance(contrato_json, str) else contrato_json
    except Exception:
        return "sin_contrato", "Error leyendo contrato"

    monto_recibido  = float(spei_data.get("monto") or 0)
    monto_esperado  = float(contrato.get("mensualidad") or 0)
    monto_total     = float(contrato.get("monto_total") or 0)
    monto_pagado    = float(lote.get("monto_pagado") or 0)

    if monto_esperado == 0:
        return "ok", None

    diferencia = abs(monto_recibido - monto_esperado)
    tolerancia = monto_esperado * 0.02  # 2% de tolerancia

    if monto_pagado + monto_recibido > monto_total and monto_total > 0:
        exceso = (monto_pagado + monto_recibido) - monto_total
        return "adelantado", f"Pago excede el total del contrato por ${exceso:,.2f}"

    if diferencia > tolerancia:
        if monto_recibido < monto_esperado:
            faltante = monto_esperado - monto_recibido
            return "monto_incorrecto", f"Pago de ${monto_recibido:,.2f} — esperado ${monto_esperado:,.2f} — falta ${faltante:,.2f}"
        else:
            exceso = monto_recibido - monto_esperado
            return "monto_incorrecto", f"Pago de ${monto_recibido:,.2f} — esperado ${monto_esperado:,.2f} — exceso ${exceso:,.2f}"

    return "ok", None
