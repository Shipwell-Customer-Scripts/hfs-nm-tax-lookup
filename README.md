# HFS NM Tax Lookup — Lambda

AWS Lambda that automatically calculates and applies New Mexico Gross Receipts Tax charges to HFS (HF Sinclair) shipments when a carrier is assigned or charge line items are updated.

---

## Overview

When a carrier is assigned to an HFS shipment originating in New Mexico, this Lambda:

1. **Fetches the shipment** via the event's `details.self_link`
2. **Validates** carrier, customer, charges, pickup stop, and origin address
3. **Looks up the NM tax rate** from a Google Sheet (`NM Tax Rates` tab) using city + county code as the primary key, falling back to county code alone
4. **Removes any existing NM Tax charge** (`charge_code == TTS`) to avoid duplicates
5. **Calculates the tax amount**: `round(sum(non-tax carrier charges) * rate, 2)`
6. **Applies the NM Tax charge** to the carrier assignment via PUT

---

## Architecture

```
Shipwell Platform
    └─ shipment.carrier_assigned / charge_line_item.* webhook
           │
           ▼
   API Gateway (rx7gy7p5z2)
           │
           ▼
   shipwell-webhook Lambda
   (routes by custom_data.name == "HFS NM Tax Lookup")
           │
           ▼
   hfs-nm-tax-lookup.fifo  (SQS FIFO)
           │
           ▼
   hfs-nm-tax-lookup  (this Lambda)
           │
           ▼
   Shipwell API (sandbox-api.shipwell.com / api.shipwell.com)
   Google Sheets API (NM Tax Rates spreadsheet)
```

**AWS Resources:**
- **Account:** `997233416332` | **Region:** `us-west-2`
- **Lambda:** `hfs-nm-tax-lookup`
- **Queue:** `hfs-nm-tax-lookup.fifo`
- **Note:** Lambda runs **outside the VPC** — no NAT required for Shipwell API calls

---

## Trigger Events

| Event | Description |
|---|---|
| `shipment.carrier_assigned` | Primary trigger — carrier just assigned |
| `shipment.charge_line_item.created` | Catches cases where charges are added after assignment |
| `shipment.charge_line_item.updated` | Catches charge updates that affect the tax base |
| `shipment.charge_line_item.carrier_added` | Carrier-side charge additions |
| `shipment.charge_line_item.carrier_updated` | Carrier-side charge updates |

**Self-loop guard:** If the event's charge code is `TTS` (the NM tax charge itself), the Lambda exits before `sw.start()` — preventing infinite loops from its own writes.

---

## Tax Rate Lookup

Rates are loaded from Google Sheets on first invocation and cached in-memory for warm Lambda reuse.

**Sheet ID:** `1Z6aOdmCri_q9UfVxPmg5lZYxl4pjg3I2kJ-uT9rhzmk`  
**Range:** `NM Tax Rates!A:E`

**Lookup logic:**
1. **Primary:** Match `UPPER(city)` + county code (combined key) → most specific rate
2. **Fallback:** Match county code only against county header rows → broader rate

**Tax formula:** `tax_amount = round(sum(non-TTS carrier charges) * rate, 2)`

---

## Validation Gates

The Lambda skips processing (no charge applied, no error) if:
- Shipment origin is not in New Mexico
- Carrier is not assigned
- No carrier charge line items exist
- City or county code cannot be determined from the pickup stop address

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | If `true`, logs writes without executing |
| `GOOGLE_SERVICE_ACCOUNT_SECRET_NAME` | `hfs-create-shipment-from-order/google-service-account` | Secrets Manager secret name for Google service account JSON |
| `GOOGLE_SERVICE_ACCOUNT_SECRET_REGION` | `us-west-2` | AWS region for the Secrets Manager secret |
| `NM_TAX_SHEET_ID` | *(see above)* | Google Sheets spreadsheet ID for tax rates |
| `NM_TAX_SHEET_RANGE` | `NM Tax Rates!A:E` | Sheet range to read |
| `SHIPWELL_API_ROOT` | `https://dev-api.shipwell.com` | Shipwell API base URL |

---

## Webhook Configuration

Register via `POST /webhooks/` (v1 endpoint):

```json
{
  "name": "HFS NM Tax Lookup",
  "url": "https://rx7gy7p5z2.execute-api.us-west-2.amazonaws.com/webhook",
  "event_names": [
    "shipment.carrier_assigned",
    "shipment.charge_line_item.created",
    "shipment.charge_line_item.updated"
  ],
  "is_enabled": true,
  "custom_data": {
    "name": "HFS NM Tax Lookup",
    "token": "Token <hfs-api-token>"
  }
}
```

---

## Deployment

### Update Lambda Code

```bash
cd /data/.openclaw/workspace/lambda/hfs-nm-tax-lookup

# Build package (dependencies already in package_build/)
cp handler.py package_build/handler.py
cd package_build
zip -r ../deploy.zip . --exclude "__pycache__/*" --exclude "*.pyc"
cd ..

aws lambda update-function-code \
  --function-name hfs-nm-tax-lookup \
  --zip-file fileb://deploy.zip \
  --region us-west-2
```

### Toggle DRY_RUN

```bash
# Live mode
aws --region us-west-2 lambda update-function-configuration \
  --function-name hfs-nm-tax-lookup \
  --environment '{"Variables":{"DRY_RUN":"false"}}'

# Safe mode
aws --region us-west-2 lambda update-function-configuration \
  --function-name hfs-nm-tax-lookup \
  --environment '{"Variables":{"DRY_RUN":"true"}}'
```

---

## Monitoring & Debugging

### CloudWatch Logs

```bash
aws logs tail /aws/lambda/hfs-nm-tax-lookup --follow --region us-west-2
```

**Key log patterns:**
- `"NM tax charge applied"` — success
- `"Skipping — origin not in NM"` — shipment not in scope
- `"Skipping — TTS self-loop"` — de-dupe guard triggered
- `"No carrier charges found"` — charges not yet present (retry via SQS)
- `"Rate not found for city/county"` — tax rate lookup miss

### Script Lab (Shipwell Workflows)

Each Lambda invocation creates a Shipwell Workflows entry on the shipment page:
- `https://sandbox.shipwell.com/shipments/{shipment_uuid}/workflows`

---

## Known Issues & Gotchas

1. **Retry adapter `connect=0`** — connection timeouts raise immediately rather than retrying (prevents exhausting the 60s Lambda timeout on Shipwell API unreachability). Script Lab errors are non-fatal; the Lambda continues to tax calculation even if the workflow log is unreachable.

2. **Charge timing** — `carrier_assigned` fires before charge line items are added by Shipwell. If no charges are present at invocation time, the Lambda logs and exits. The subsequent `charge_line_item.created` event triggers a second pass that applies the tax.

3. **VPC** — this Lambda runs outside the VPC. Do not add it to the VPC without also configuring NAT Gateway access.

4. **Google Sheets cache** — tax rates are cached in-memory for the Lambda's warm lifetime. If rates change in the sheet, a cold start (or Lambda redeploy) is needed to pick up the new values.

---

## Files

| File | Description |
|---|---|
| `handler.py` | Main Lambda handler |
| `package_build/` | Deploy package directory (includes dependencies) |
