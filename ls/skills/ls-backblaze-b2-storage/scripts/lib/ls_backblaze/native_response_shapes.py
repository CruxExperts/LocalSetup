"""Safe native administration result fields from the reviewed B2 v4 reference."""
from __future__ import annotations

from .request_shapes import obj, array, string, integer

TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
STRINGS = array(TEXT)
ALLOWED = obj({"buckets": {"type": ["array", "null"], "items": obj({"id": TEXT, "name": NULL_TEXT}, ("id", "name"))}, "capabilities": STRINGS, "namePrefix": NULL_TEXT}, ("capabilities",))
STORAGE = obj({"allowed": ALLOWED, "apiUrl": TEXT, "downloadUrl": TEXT, "s3ApiUrl": TEXT, "absoluteMinimumPartSize": INT, "recommendedPartSize": INT})
AUTH = obj({"account_id": TEXT, "allowed": ALLOWED, "api_info": obj({"storageApi": STORAGE})}, ("account_id", "allowed", "api_info"))
RETENTION = obj({"mode": NULL_TEXT, "period": {"oneOf": [{"type": "null"}, obj({"duration": INT, "unit": TEXT})]}})
ENCRYPTION = obj({"isClientAuthorizedToRead": BOOL, "value": obj({"mode": NULL_TEXT, "algorithm": NULL_TEXT})})
CORS = obj({"corsRuleName": TEXT, "allowedOrigins": STRINGS, "allowedHeaders": STRINGS, "allowedOperations": STRINGS, "exposeHeaders": STRINGS, "maxAgeSeconds": INT})
LIFECYCLE = obj({"fileNamePrefix": TEXT, "lifecycleRuleId": TEXT, **{name: {"type": ["integer", "null"]} for name in ("daysFromHidingToDeleting", "daysFromUploadingToHiding", "daysFromStartingToCancelingUnfinishedLargeFiles")}})
BUCKET = obj({"accountId": TEXT, "bucketId": TEXT, "bucketName": TEXT, "bucketType": TEXT, "bucketInfo": {"type": "object", "additionalProperties": True}, "corsRules": array(CORS), "lifecycleRules": array(LIFECYCLE), "revision": INT, "options": STRINGS, "defaultServerSideEncryption": ENCRYPTION, "fileLockConfiguration": obj({"isClientAuthorizedToRead": BOOL, "value": obj({"defaultRetention": RETENTION, "isFileLockEnabled": BOOL})})}, ("bucketId",))
KEY = obj({"accountId": TEXT, "applicationKeyId": TEXT, "keyName": TEXT, "capabilities": STRINGS, "expirationTimestamp": {"type": ["integer", "null"]}, "bucketIds": {"type": ["array", "null"], "items": TEXT}, "namePrefix": NULL_TEXT, "options": STRINGS}, ("applicationKeyId",))
TARGET = obj({"targetType": TEXT, "url": TEXT, "maxEventsPerBatch": INT})
RULE = obj({"name": TEXT, "eventTypes": STRINGS, "isEnabled": BOOL, "objectNamePrefix": TEXT, "targetConfiguration": TARGET, "isSuspended": BOOL, "suspensionReason": NULL_TEXT})
NOTIFICATIONS = array(obj({"bucketId": TEXT, "eventNotificationRules": array(RULE)}, ("bucketId", "eventNotificationRules")))


def payload(name: str, *, secret: bool = False):
    if name == "AuthorizeAccount":
        return AUTH
    if name in {"GetNotificationRules", "SetNotificationRules"}:
        return NOTIFICATIONS
    if name == "ListBuckets":
        return obj({"buckets": array(BUCKET)}, ("buckets",))
    if name == "ListKeys":
        return obj({"keys": array(KEY), "nextApplicationKeyId": NULL_TEXT}, ("keys",))
    if name in {"CreateKey", "DeleteKey"}:
        if name == "CreateKey" and secret:
            return {**KEY, "properties": {**KEY["properties"], "applicationKey": TEXT}}
        return KEY
    return BUCKET
