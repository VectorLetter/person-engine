"""Safe HTTP diagnostics never serialize exception text or input values."""
from ..core_io import FoundationError

MESSAGES = {
    'WORKSPACE_NOT_FOUND': 'Workspace not found.', 'JOB_NOT_FOUND': 'Job not found.',
    'NOT_FOUND': 'Resource not found in this workspace.',
    'WORKSPACE_EXISTS': 'Core initialization requires a new or empty workspace.',
    'ASSET_CONFLICT': 'Registration conflicts with an existing binding or request.',
    'CONFLICT': 'Request conflicts with existing core state.',
    'STALE_REVISION': 'The supplied revision or source basis is stale.',
    'INVALID_DECISION': 'The decision does not satisfy the core contract.',
    'MODEL_UNAVAILABLE': 'No local model provider is configured.',
    'CALIBRATION_REQUIRED': 'Explicit candidate policy is required.',
    'UNSAFE_PATH': 'Source must be a regular file inside this workspace import directory.',
    'INVALID_REQUEST': 'The request does not satisfy the API contract.',
    'RUN_FAILED': 'Local job failed. Inspect local operator diagnostics.',
    'INTERNAL_ERROR': 'The operation failed.',
    'WRITER_BUSY': 'Workspace writer is busy.',
    'REQUEST_TOO_LARGE': 'Request body exceeds the local API limit.',
    'LOCAL_ONLY': 'Only same-origin loopback requests are supported.',
    'JOB_LIMIT': 'The in-memory job limit has been reached.',
}


# Closed public vocabulary: arbitrary provider/core exception codes never cross HTTP.
ERROR_STATUS = dict.fromkeys((
    'INVALID_REQUEST', 'INVALID_DECISION', 'UNSAFE_PATH', 'CONTENT_LIMIT', 'UNSUPPORTED_MEDIA',
    'INCOMPATIBLE_REQUEST', 'INCOMPATIBLE_WORKSPACE', 'INVALID_WORKSPACE', 'CONFIG_REQUIRED',
    'MODEL_CHANGED', 'MODEL_CONTRACT', 'DETECTION_INVALID', 'EMBEDDING_INVALID', 'DECODE_FAILED',
    'CALIBRATION_REQUIRED'), 422)
ERROR_STATUS.update(dict.fromkeys(('NOT_FOUND', 'WORKSPACE_NOT_FOUND', 'JOB_NOT_FOUND'), 404))
ERROR_STATUS.update(dict.fromkeys((
    'CONFLICT', 'ASSET_CONFLICT', 'WORKSPACE_EXISTS', 'STALE_REVISION', 'FROZEN_REVISION',
    'CORRECTION_REQUIRED', 'MODEL_SPACE_CONFLICT', 'SOURCE_CHANGED', 'WORKSPACE_MISMATCH'), 409))
ERROR_STATUS.update(dict.fromkeys(('WRITER_BUSY', 'MODEL_UNAVAILABLE', 'JOB_LIMIT'), 503))
ERROR_STATUS.update(dict.fromkeys(('INTERNAL_ERROR', 'RUN_FAILED', 'SNAPSHOT_INVALID'), 500))
ERROR_STATUS.update(LOCAL_ONLY=403, REQUEST_TOO_LARGE=413, HTTP_ERROR=405)
MESSAGES.update(HTTP_ERROR='HTTP request rejected.', SNAPSHOT_INVALID='Published snapshot integrity validation failed.')
PUBLIC_V0_ERROR_CODE_TABLE = {code: {'http_status': status,
    'message': MESSAGES.get(code, 'The core contract rejected this operation.')}
    for code, status in sorted(ERROR_STATUS.items())}


def describe(exc, request_id, *, job=False, registration=False):
    fallback = 'RUN_FAILED' if job else 'INTERNAL_ERROR'
    code = exc.code if isinstance(exc, FoundationError) else fallback
    if code == 'STALE_SOURCE':
        code = 'STALE_REVISION'
    if registration and code == 'CONFLICT':
        code = 'ASSET_CONFLICT'
    if code not in PUBLIC_V0_ERROR_CODE_TABLE:
        code = fallback
    entry = PUBLIC_V0_ERROR_CODE_TABLE[code]
    return entry['http_status'], {'code': code, 'message': entry['message'],
                                 'request_id': request_id, 'details': None}
