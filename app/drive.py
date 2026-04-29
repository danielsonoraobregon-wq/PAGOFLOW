import os
import json
import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from typing import Optional

# Scope de solo escritura — NO puede leer ni listar tu Drive
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_drive_service = None


def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service

    # Las credenciales vienen de variable de entorno (nunca en el código)
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON no configurado")

    creds_dict = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES
    )
    _drive_service = build("drive", "v3", credentials=credentials)
    return _drive_service


def get_or_create_folder(service, folder_name: str, parent_id: str) -> str:
    """Busca la carpeta dentro del parent. Si no existe, la crea."""
    query = (
        f"name='{folder_name}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        f"and trashed=false"
    )
    results = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        # Solo busca entre archivos que la cuenta de servicio creó (drive.file scope)
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # No existe, la crea
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(
        body=folder_metadata, fields="id"
    ).execute()
    return folder["id"]


async def upload_to_drive(
    file_bytes: bytes,
    filename: str,
    folder_path: str,
    mime_type: str = "image/jpeg",
) -> bool:
    """
    Sube el archivo a la ruta folder_path dentro del Drive compartido.
    Solo usa drive.file scope — no puede leer nada existente.
    """
    try:
        service = get_drive_service()

        # ID de la carpeta raíz compartida contigo (configuras en .env)
        root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID")
        if not root_folder_id:
            raise ValueError("DRIVE_ROOT_FOLDER_ID no configurado")

        # Crear estructura de carpetas: Lotes/Lote-12/Comprobantes
        parts = folder_path.strip("/").split("/")
        current_parent = root_folder_id
        for part in parts:
            current_parent = get_or_create_folder(service, part, current_parent)

        # Subir archivo
        media = MediaInMemoryUpload(file_bytes, mimetype=mime_type, resumable=False)
        file_metadata = {
            "name": filename,
            "parents": [current_parent],
        }
        service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        ).execute()

        print(f"✅ Subido a Drive: {folder_path}/{filename}")
        return True

    except Exception as e:
        print(f"❌ Error subiendo a Drive: {e}")
        return False
