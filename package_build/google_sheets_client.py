"""Small Google Sheets API wrapper for HAC capacity integration."""

import json
import logging
from importlib import import_module
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from config import GOOGLE_SERVICE_ACCOUNT_SECRET_NAME, GOOGLE_SERVICE_ACCOUNT_SECRET_REGION

logger = logging.getLogger()

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class GoogleSheetsClient:
    """Direct HTTP Google Sheets client backed by a service account credential."""

    def __init__(self, service_account_info: Dict[str, Any]):
        if not service_account_info:
            raise ValueError("Google service account info is required")
        service_account = import_module("google.oauth2.service_account")
        auth_requests = import_module("google.auth.transport.requests")
        self._credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=[SHEETS_SCOPE, DRIVE_SCOPE],
        )
        self._auth_request = auth_requests.Request()

    def _access_token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(self._auth_request)
        return self._credentials.token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

    def get_values(self, spreadsheet_id: str, range_name: str) -> List[List[Any]]:
        """Read values from a Google Sheet range."""
        response = requests.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_name}",
            headers=self._headers(),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("values") or []

    def update_values(
        self, spreadsheet_id: str, range_name: str, values: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Write values to a Google Sheet range."""
        encoded_range = quote(range_name, safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}"
        payload = {"values": values}
        logger.info(f"update_values: PUT {url} | range={range_name!r} | values={values!r}")
        response = requests.put(
            url,
            headers=self._headers(),
            params={"valueInputOption": value_input_option},
            data=json.dumps(payload),
            timeout=30,
        )
        logger.info(f"update_values: response {response.status_code} — {response.text[:300]!r}")
        response.raise_for_status()
        return response.json()

    def batch_update_values(
        self, spreadsheet_id: str, updates: List[Dict[str, Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Write multiple ranges in one API request."""
        response = requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate",
            headers=self._headers(),
            data=json.dumps({"valueInputOption": value_input_option, "data": updates}),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


def load_service_account_info(secret_name: Optional[str] = None, region_name: Optional[str] = None) -> Dict[str, Any]:
    """Load a Google service account JSON object from AWS Secrets Manager."""
    resolved_secret_name = secret_name or GOOGLE_SERVICE_ACCOUNT_SECRET_NAME
    if not resolved_secret_name:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_SECRET_NAME is not configured")

    boto3 = import_module("boto3")
    client = boto3.client("secretsmanager", region_name=region_name or GOOGLE_SERVICE_ACCOUNT_SECRET_REGION)
    response = client.get_secret_value(SecretId=resolved_secret_name)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise ValueError(f"Secret {resolved_secret_name} did not contain SecretString")
    service_account_info = json.loads(secret_string)
    if not isinstance(service_account_info, dict):
        raise ValueError(f"Secret {resolved_secret_name} must contain a JSON object")
    return service_account_info


def build_google_sheets_client() -> GoogleSheetsClient:
    """Build the configured Google Sheets client."""
    return GoogleSheetsClient(load_service_account_info())
