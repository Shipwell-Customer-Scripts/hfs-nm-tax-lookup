"""Support notification helpers."""

import hashlib
import logging
import time
from importlib import import_module
from typing import Optional, List

import os
from config import (
    DRY_RUN,
    EMAIL_PROVIDER,
    POSTMARK_API_TOKEN,
    SEND_SUPPORT_EMAILS,
    SUPPORT_EMAIL,
    SUPPORT_EMAIL_FROM,
    SUPPORT_EMAIL_OVERRIDE,
)

SES_REGION = os.environ.get("SES_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))

# Cooldown in seconds between repeated emails for the same subject+order.
# Prevents flooding when an order is stuck in a retry loop.
EMAIL_COOLDOWN_SECONDS = int(os.environ.get("EMAIL_COOLDOWN_SECONDS", "300"))  # 5 min default

# DynamoDB table for cross-instance/cross-cold-start email dedup.
EMAIL_DEDUP_TABLE = os.environ.get("EMAIL_DEDUP_TABLE", "hfs-email-dedup")
DYNAMO_REGION = os.environ.get("DYNAMO_REGION", "us-west-2")

# In-memory dedup cache: {dedup_key: sent_at_epoch}
# L1 cache — fast, but resets on cold start. DynamoDB (L2) covers cross-instance gaps.
_email_sent_cache: dict = {}

logger = logging.getLogger()


def notify_support(
    subject: str,
    body: str,
    sw: Optional["ShipwellProgram"] = None,
    html_body: Optional[str] = None,
) -> None:
    if SUPPORT_EMAIL_OVERRIDE:
        recipients: List[str] = [r.strip() for r in SUPPORT_EMAIL_OVERRIDE.split(",") if r.strip()]
    else:
        recipients = [SUPPORT_EMAIL]

    if sw:
        sw.log("WARNING", f"{subject}\n{body}", ["notify_support"])

    if not SEND_SUPPORT_EMAILS:
        logger.info(f"Support notification logged only (would send to {recipients}): {subject}")
        return

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would email {recipients}: {subject}\n{body}")
        return

    dedup_key = hashlib.md5(subject.encode()).hexdigest()
    now = time.time()

    # L1: in-memory cache (fast, within same Lambda instance)
    last_sent = _email_sent_cache.get(dedup_key, 0)
    if now - last_sent < EMAIL_COOLDOWN_SECONDS:
        logger.info(f"Suppressing duplicate email (in-memory cooldown {EMAIL_COOLDOWN_SECONDS}s): {subject}")
        return

    # L2: DynamoDB cache (survives cold starts, cross-instance dedup)
    # Uses a conditional PutItem — succeeds only if the key doesn't already exist.
    # ConditionalCheckFailedException → duplicate, suppress. Any other error → fail open.
    _dynamo_dedup_suppressed = False
    try:
        import boto3
        dynamo = boto3.client("dynamodb", region_name=DYNAMO_REGION)
        expires_at = int(now) + EMAIL_COOLDOWN_SECONDS
        dynamo.put_item(
            TableName=EMAIL_DEDUP_TABLE,
            Item={
                "dedup_key": {"S": dedup_key},
                "subject": {"S": subject[:500]},
                "expires_at": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(dedup_key)",
        )
        # Successfully wrote — not a duplicate, proceed with send
        _email_sent_cache[dedup_key] = now
    except Exception as dynamo_err:
        err_str = str(dynamo_err)
        if "ConditionalCheckFailedException" in err_str:
            logger.info(f"Suppressing duplicate email (DynamoDB cooldown {EMAIL_COOLDOWN_SECONDS}s): {subject}")
            _email_sent_cache[dedup_key] = now  # sync L1 cache to avoid redundant DDB hits
            _dynamo_dedup_suppressed = True
        else:
            # DynamoDB unavailable or misconfigured — log warning but fail open (send the email)
            logger.warning(f"DynamoDB dedup check failed (will send email): {dynamo_err}")
            _email_sent_cache[dedup_key] = now

    if _dynamo_dedup_suppressed:
        return

    if EMAIL_PROVIDER == "postmark":
        _send_via_postmark(subject, body, recipients, html_body)
    else:
        _send_via_ses(subject, body, recipients, html_body)


def _send_via_ses(
    subject: str,
    body: str,
    recipients: List[str],
    html_body: Optional[str] = None,
) -> None:
    """Send email via AWS SES."""
    try:
        boto3 = import_module("boto3")
        ses = boto3.client("ses", region_name=SES_REGION)
        email_body: dict = {"Text": {"Data": body, "Charset": "UTF-8"}}
        if html_body:
            email_body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
        ses.send_email(
            Source=SUPPORT_EMAIL_FROM,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": email_body,
            },
        )
        logger.info(f"Support email sent via SES to {recipients}: {subject}")
    except Exception as e:
        logger.error(f"Failed to send support email via SES '{subject}': {e}", exc_info=True)


def _send_via_postmark(
    subject: str,
    body: str,
    recipients: List[str],
    html_body: Optional[str] = None,
) -> None:
    """Send email via Postmark API."""
    if not POSTMARK_API_TOKEN:
        logger.error("POSTMARK_API_TOKEN is not set — cannot send email via Postmark")
        return
    try:
        requests = import_module("requests")
        payload: dict = {
            "From": SUPPORT_EMAIL_FROM,
            "To": ", ".join(recipients),
            "Subject": subject,
            "TextBody": body,
            "MessageStream": "outbound",
        }
        if html_body:
            payload["HtmlBody"] = html_body
        resp = requests.post(
            "https://api.postmarkapp.com/email",
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": POSTMARK_API_TOKEN,
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("ErrorCode", 0) != 0:
            logger.error(f"Postmark rejected email '{subject}': {result.get('Message')} (code {result['ErrorCode']})")
        else:
            logger.info(f"Support email sent via Postmark to {recipients}: {subject} (MessageID={result.get('MessageID')})")
    except Exception as e:
        logger.error(f"Failed to send support email via Postmark '{subject}': {e}", exc_info=True)
