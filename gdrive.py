"""Google Drive helper — list inputs, download PDFs, upload outputs.

Authenticates via OAuth token stored in Streamlit secrets or local token.json.
Auto-refreshes expired tokens.
"""
import io
import json
import logging
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

# GDrive folder IDs (01_tenders structure)
TENDERS_ROOT_ID = "13eoikwAQPQkTTymdEQpmXbwu2s7b8gtq"   # 01_tenders/
INPUTS_FOLDER_ID = "1Y6vnbBYjyjPfIOEgKS2gPBnT5PCqXF6B"   # 01_tenders/inputs/


def get_drive_service():
    """Build authenticated Drive API service.

    Tries Streamlit secrets first, then local token.json.
    """
    creds = None

    # Try Streamlit secrets
    try:
        import streamlit as st
        if "GDRIVE_TOKEN" in st.secrets:
            token_data = dict(st.secrets["GDRIVE_TOKEN"])
            # st.secrets returns AttrDict — convert scopes to list
            if "scopes" in token_data:
                token_data["scopes"] = list(token_data["scopes"])
            creds = Credentials.from_authorized_user_info(token_data)
            logger.info("GDrive credentials loaded from st.secrets")
    except Exception as e:
        logger.error(f"Failed to load GDrive credentials from st.secrets: {e}")
        import traceback
        traceback.print_exc()

    # Fallback: local token file
    if creds is None:
        token_path = Path(__file__).parent / "token.json"
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path))
            logger.info("GDrive credentials loaded from token.json")

    if creds is None:
        # Show available secrets keys for debugging
        debug_info = "unknown"
        try:
            import streamlit as st
            debug_info = f"secrets keys: {list(st.secrets.keys())}"
            if "GDRIVE_TOKEN" in st.secrets:
                debug_info += f" | GDRIVE_TOKEN keys: {list(dict(st.secrets['GDRIVE_TOKEN']).keys())}"
        except Exception as e2:
            debug_info = f"secrets access error: {e2}"
        raise RuntimeError(f"No GDrive credentials found. Debug: {debug_info}")

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        logger.info("GDrive token refreshed")

    return build("drive", "v3", credentials=creds)


def list_input_folders(service=None) -> list[dict]:
    """List tender input folders (subfolders of inputs/)."""
    if service is None:
        service = get_drive_service()

    results = service.files().list(
        q=f"'{INPUTS_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        pageSize=100,
        fields="files(id, name, modifiedTime)",
        orderBy="modifiedTime desc",
    ).execute()

    return results.get("files", [])


def list_folder_pdfs(folder_id: str, service=None) -> list[dict]:
    """List PDF files in a folder."""
    if service is None:
        service = get_drive_service()

    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
        pageSize=50,
        fields="files(id, name, size, modifiedTime)",
        orderBy="name",
    ).execute()

    return results.get("files", [])


def download_pdf(file_id: str, service=None) -> bytes:
    """Download a PDF file by ID, return bytes."""
    if service is None:
        service = get_drive_service()

    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def _get_or_create_folder(name: str, parent_id: str, service) -> str:
    """Get existing folder by name under parent, or create it. Returns folder ID."""
    results = service.files().list(
        q=f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        pageSize=1,
        fields="files(id)",
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    logger.info(f"Created GDrive folder: {name} ({folder['id']})")
    return folder["id"]


def save_output(file_bytes: bytes, filename: str, tender_name: str, mime_type: str, service=None) -> str:
    """Save an output file to GDrive under 01_tenders/outputs/{tender_name}/.

    Returns the file ID.
    """
    if service is None:
        service = get_drive_service()

    # Ensure outputs/ folder exists
    outputs_folder_id = _get_or_create_folder("outputs", TENDERS_ROOT_ID, service)

    # Ensure tender subfolder exists
    tender_folder_id = _get_or_create_folder(tender_name, outputs_folder_id, service)

    # Upload file
    metadata = {
        "name": filename,
        "parents": [tender_folder_id],
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    uploaded = service.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()

    logger.info(f"Uploaded to GDrive: {filename} ({uploaded['id']})")
    return uploaded.get("webViewLink", uploaded["id"])
