import os
import httpx
from typing import Optional, Tuple

WA_TOKEN   = os.getenv("WA_TOKEN")
PHONE_ID   = os.getenv("WA_PHONE_NUMBER_ID")
WA_API_URL = "https://graph.facebook.com/v19.0"


async def download_media(media_id: str) -> Tuple[Optional[bytes], str]:
    """Descarga imagen/PDF de los servidores de Meta."""
    try:
        headers = {"Authorization": f"Bearer {WA_TOKEN}"}

        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Obtener URL de descarga
            meta_resp = await client.get(
                f"{WA_API_URL}/{media_id}",
                headers=headers,
            )
            if meta_resp.status_code != 200:
                print(f"Error obteniendo media URL: {meta_resp.text}")
                return None, ""

            media_info = meta_resp.json()
            media_url  = media_info.get("url")
            mime_type  = media_info.get("mime_type", "image/jpeg")

            # 2. Descargar el archivo
            file_resp = await client.get(media_url, headers=headers)
            if file_resp.status_code != 200:
                print(f"Error descargando media: {file_resp.status_code}")
                return None, ""

            return file_resp.content, mime_type

    except Exception as e:
        print(f"Error en download_media: {e}")
        return None, ""


async def send_message(to_phone: str, text: str) -> bool:
    """Manda mensaje de WhatsApp al número indicado."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{WA_API_URL}/{PHONE_ID}/messages",
                headers={
                    "Authorization": f"Bearer {WA_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": to_phone,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            return response.status_code == 200
    except Exception as e:
        print(f"Error enviando mensaje: {e}")
        return False
