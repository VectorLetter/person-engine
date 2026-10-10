"""Versioned localhost HTTP adapter for the frozen Public V0 core."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import uuid

from fastapi import FastAPI, Request, Response, Query, Header, Depends
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import public_v0 as core
from ..core_io import FoundationError, canonical
from ..public_v0.store import token, require
from . import models as m
from .errors import describe
from .service import Service


def create_app(root: Path | str, *, provider=None):
    """Provider injection is operator/Python-only, never a client module/path loader."""
    @asynccontextmanager
    async def lifespan(app):
        service = await run_in_threadpool(Service, root, provider)
        app.state.service = service
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

    def request_headers(x_request_id: m.Token | None = Header(None, description='Optional request correlation token; generated when absent.')):
        pass

    def mutation_header(idempotency_key: m.Token = Header(..., description='Required retry key, scoped to workspace and operation.')):
        return idempotency_key

    def workspace_header(idempotency_key: m.Token | None = Header(None, description='If supplied, must equal workspace_key. Existing workspace creation conflicts.')):
        return idempotency_key

    errors = {n: {'model': m.ErrorView} for n in (400, 403, 404, 405, 409, 413, 415, 422, 500, 503)}
    app = FastAPI(title='Person Engine Local API', version='1.0.0', lifespan=lifespan,
                  responses=errors, redoc_url=None, dependencies=[Depends(request_headers)])

    def failure(request, exc):
        status, body = describe(exc, request.state.request_id,
                                registration=request.url.path.endswith('/assets'))
        return JSONResponse(body, status_code=status, headers={'X-Request-ID': request.state.request_id})

    @app.middleware('http')
    async def boundary(request, call_next):
        supplied = request.headers.get('X-Request-ID')
        request.state.request_id = supplied if supplied and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', supplied) else uuid.uuid4().hex
        try:
            require(supplied is None or supplied == request.state.request_id, 'INVALID_REQUEST', 'Invalid request id')
            require(request.url.hostname in {'localhost', '127.0.0.1', '::1'}, 'LOCAL_ONLY', 'Loopback host required')
            origin = request.headers.get('origin')
            require(origin is None or origin == str(request.base_url).rstrip('/'), 'LOCAL_ONLY', 'Same origin required')
            # Bounded JSON bodies; multipart and binary uploads have no routes.
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                require(size <= 1024 * 1024, 'REQUEST_TOO_LARGE', 'Body limit')
                chunks.append(chunk)
            request._body = b''.join(chunks)
            response = await call_next(request)
        except Exception as exc:
            response = failure(request, exc)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(FoundationError)
    async def core_error(request, exc):
        return failure(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        is_decision_body = request.url.path.endswith('/decisions') and any(e['loc'][0] == 'body' for e in exc.errors())
        code = 'INVALID_DECISION' if is_decision_body else 'INVALID_REQUEST'
        return failure(request, FoundationError(code, 'Invalid request'))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 404:
            return failure(request, FoundationError('NOT_FOUND', 'Unknown route'))
        body = {'code': 'HTTP_ERROR', 'message': 'HTTP request rejected.', 'request_id': request.state.request_id, 'details': None}
        return JSONResponse(body, status_code=exc.status_code)

    def service():
        return app.state.service

    def key(request):
        value = request.headers.get('Idempotency-Key')
        # Correlation ID is not silently promoted into a retry key.
        require(value is not None, 'INVALID_REQUEST', 'Idempotency-Key required')
        return token(value, 'Idempotency-Key')

    def envelope(request, data):
        return {'request_id': request.state.request_id, 'data': data}

    def page(items, offset, limit):
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit}

    @app.get('/health', operation_id='health', description='Service liveness only; does not assert workspace or model readiness.', response_model=m.Health)
    def health():
        return {'status': 'ok'}

    @app.post('/v1/workspaces', operation_id='create_workspace', description='Create a fresh local workspace. Core assigns workspace_id. Repeating workspace_key returns 409 WORKSPACE_EXISTS; creation has no replay receipt.', dependencies=[Depends(workspace_header)], response_model=m.Envelope[m.WorkspaceView], status_code=201)
    def create_workspace(body: m.WorkspaceCreate, request: Request):
        supplied = request.headers.get('Idempotency-Key')
        require(supplied is None or token(supplied) == body.workspace_key, 'INVALID_REQUEST', 'Workspace key mismatch')
        return envelope(request, service().create_workspace(body.workspace_key))

    @app.get('/v1/workspaces/{workspace_id}', operation_id='workspace', description='Read workspace identity, current source basis and the relative local import directory. No database or absolute filesystem path is exposed.', response_model=m.Envelope[m.WorkspaceView])
    def workspace(workspace_id: m.Token, request: Request):
        return envelope(request, service().workspace_view(service().workspace(workspace_id)))

    @app.post('/v1/workspaces/{workspace_id}/assets', operation_id='register', description='Register a logical source binding using a workspace-relative local file. Equal content may belong to distinct logical assets. Returns 201 CREATED or 200 REPLAY with the core receipt; conflicts return 409.', dependencies=[Depends(mutation_header)], response_model=m.Envelope[m.RegistrationView], status_code=201,
              responses={200: {'model': m.Envelope[m.RegistrationView]}})
    def register(workspace_id: m.Token, body: m.AssetCreate, request: Request, response: Response):
        data = service().register(workspace_id, body, key(request))
        response.status_code = 200 if data['replay'] else 201
        return envelope(request, data)

    @app.post('/v1/workspaces/{workspace_id}/ingest-runs', operation_id='run', description='Queue local ingest followed by non-authoritative matching. Returns 202 or 200 for a same-key in-process replay. Jobs do not survive restart; old IDs return 404 JOB_NOT_FOUND.', dependencies=[Depends(mutation_header)], response_model=m.Envelope[m.JobView], status_code=202,
              responses={200: {'model': m.Envelope[m.JobView]}})
    def run(workspace_id: m.Token, body: m.RunCreate, request: Request, response: Response):
        data, replay = service().enqueue(workspace_id, body, key(request), request.state.request_id)
        response.status_code = 200 if replay else 202
        return envelope(request, data)

    @app.get('/v1/workspaces/{workspace_id}/jobs/{job_id}', operation_id='job', description='Read ephemeral job lifecycle and stage progress. Successful result_refs.run_id identifies a durable core result. After restart old job IDs always return JOB_NOT_FOUND.', response_model=m.Envelope[m.JobView])
    def job(workspace_id: m.Token, job_id: m.Token, request: Request):
        return envelope(request, service().job(workspace_id, job_id))

    @app.get('/v1/workspaces/{workspace_id}/people', operation_id='people', description='Read human-created people with offset/limit pagination; no implicit machine identities.', response_model=m.Envelope[m.Page[m.Person]])
    def people(workspace_id: m.Token, request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=512)):
        return envelope(request, page(service().workspace(workspace_id).inspect()['persons'], offset, limit))

    @app.get('/v1/workspaces/{workspace_id}/people/{person_id}', operation_id='person', description='Read one human-created person scoped to this workspace.', response_model=m.Envelope[m.Person])
    def person(workspace_id: m.Token, person_id: m.Token, request: Request):
        items = service().workspace(workspace_id).inspect()['persons']
        item = next((p for p in items if p['person_id'] == person_id), None)
        require(item is not None, 'NOT_FOUND', 'Unknown person')
        return envelope(request, item)

    @app.get('/v1/workspaces/{workspace_id}/faces', operation_id='faces', description='Read public face geometry and human state, optionally filtered by asset_id. Detector score is uncalibrated evidence; no raw embeddings are returned.', response_model=m.Envelope[m.Page[m.Face]])
    def faces(workspace_id: m.Token, request: Request, asset_id: m.Token | None = None,
              offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=512)):
        items = service().workspace(workspace_id).inspect()['faces']
        if asset_id is not None:
            items = [f for f in items if f['asset_id'] == asset_id]
        return envelope(request, page(items, offset, limit))

    @app.get('/v1/workspaces/{workspace_id}/assignments', operation_id='assignments', description='Read current human-confirmed face/person assignments, optionally filtered by person_id. Unknown, ignored, outsider and unassigned faces are excluded.', response_model=m.Envelope[m.Page[m.Assignment]])
    def assignments(workspace_id: m.Token, request: Request, person_id: m.Token | None = None,
                    offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=512)):
        ws = service().workspace(workspace_id)
        with ws.read() as c:
            items = [dict(r) for r in c.execute("SELECT face_id,person_id,event_id FROM human_state WHERE state='ASSIGNED' ORDER BY face_id")]
        if person_id is not None:
            items = [a for a in items if a['person_id'] == person_id]
        return envelope(request, page(items, offset, limit))

    @app.post('/v1/workspaces/{workspace_id}/reviews', operation_id='review_create', description='Freeze a core run into a review bound to workspace source basis and review revision. Same-key replay returns the original creation result (201); GET retrieves the current review.', dependencies=[Depends(mutation_header)], response_model=m.Envelope[m.ReviewView], status_code=201, response_model_exclude_unset=True)
    def review_create(workspace_id: m.Token, body: m.ReviewCreate, request: Request):
        with service().mutation(workspace_id) as ws:
            return envelope(request, core.create_session(ws, body.run_id, key(request)))

    @app.get('/v1/workspaces/{workspace_id}/reviews/{review_id}', operation_id='review_get', description='Read the current review. session_id is the review_id resource identifier, retained for core receipt compatibility. Candidates bind to the frozen source basis.', response_model=m.Envelope[m.ReviewView], response_model_exclude_unset=True)
    def review_get(workspace_id: m.Token, review_id: m.Token, request: Request):
        return envelope(request, core.fetch_session(service().workspace(workspace_id), review_id))

    @app.get('/v1/workspaces/{workspace_id}/reviews/{review_id}/candidates', operation_id='candidates', description='Read frozen review candidates. Scores and provisional groups are non-authoritative, uncalibrated evidence; neither establishes identity.', response_model=m.Envelope[m.Page[m.Candidate]])
    def candidates(workspace_id: m.Token, review_id: m.Token, request: Request,
                   offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=512)):
        data = core.fetch_session(service().workspace(workspace_id), review_id)['source']['candidates']
        return envelope(request, page(data, offset, limit))

    @app.post('/v1/workspaces/{workspace_id}/reviews/{review_id}/decisions', operation_id='decision', description='Save a complete decision list as a new draft revision using core validation. Same-key replay returns the original save receipt without advancing revision. Explicit null enrollment is invalid.', dependencies=[Depends(mutation_header)], response_model=m.Envelope[m.DecisionReceipt])
    def decision(workspace_id: m.Token, review_id: m.Token, body: m.DecisionSave, request: Request):
        # Only omit absent optional fields; never rewrite actions or expected state.
        decisions = [d.model_dump(exclude_unset=True) for d in body.decisions]
        with service().mutation(workspace_id) as ws:
            return envelope(request, core.save_decisions(ws, review_id, body.revision, decisions, key(request)))

    @app.post('/v1/workspaces/{workspace_id}/reviews/{review_id}/apply', operation_id='apply', description='Atomically apply the specified review revision. X-Apply-Outcome distinguishes SUCCESS and IDEMPOTENT_REPLAY; both return the unchanged core receipt. Stale/conflict=409, invalid=422, internal failure=500 with rollback.', dependencies=[Depends(mutation_header)], responses={200: {'headers': {'X-Apply-Outcome': {'description': 'Core apply outcome; receipt bytes and identity are preserved.', 'schema': {'type': 'string', 'enum': ['SUCCESS', 'IDEMPOTENT_REPLAY']}}}}}, response_model=m.Envelope[m.ApplyReceipt])
    def apply(workspace_id: m.Token, review_id: m.Token, body: m.ApplyRequest, request: Request, response: Response):
        with service().mutation(workspace_id) as ws:
            prior = core.fetch_session(ws, review_id)
            result = core.apply(ws, review_id, body.revision, key(request))
            response.headers['X-Apply-Outcome'] = 'IDEMPOTENT_REPLAY' if prior['status'] == 'APPLIED' else 'SUCCESS'
            return envelope(request, result)

    @app.post('/v1/workspaces/{workspace_id}/snapshots', operation_id='publish', description='Publish an immutable versioned Public V0 snapshot from expected_basis. Same-key replay returns the original receipt (201); a new key may create a new version at the same basis.', dependencies=[Depends(mutation_header)], response_model=m.Envelope[m.SnapshotReceipt], status_code=201)
    def publish(workspace_id: m.Token, body: m.SnapshotCreate, request: Request):
        with service().mutation(workspace_id) as ws:
            return envelope(request, core.publish_snapshot(ws, key(request), body.expected_basis.model_dump()))

    @app.get('/v1/workspaces/{workspace_id}/snapshots', operation_id='snapshots', description='List publication receipts ordered by increasing snapshot_version with offset/limit pagination.', response_model=m.Envelope[m.Page[m.SnapshotReceipt]])
    def snapshots(workspace_id: m.Token, request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=512)):
        with service().workspace(workspace_id).read() as c:
            items = [json.loads(r[0]) for r in c.execute('SELECT receipt FROM snapshot ORDER BY version')]
        return envelope(request, page(items, offset, limit))

    @app.get('/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}', operation_id='snapshot_get', description='Read and verify one immutable Public V0 snapshot. Snapshot schema and producer versions belong to the existing core contract.', response_model=m.Envelope[m.SnapshotDocument])
    def snapshot_get(workspace_id: m.Token, snapshot_id: m.Token, request: Request):
        return envelope(request, core.read_snapshot(service().workspace(workspace_id), snapshot_id))

    @app.get('/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/export', operation_id='export', description='Download canonical Public V0 snapshot JSON with trailing newline. Bytes match payload_sha256. The core may materialize its immutable local export; no new snapshot or domain revision is created.', response_model=m.SnapshotDocument)
    def export(workspace_id: m.Token, snapshot_id: m.Token):
        with service().mutation(workspace_id) as ws:
            # Core creates/verifies immutable export; send canonical public JSON only.
            core.export_snapshot(ws, snapshot_id)
            doc = core.read_snapshot(ws, snapshot_id)
        return Response(canonical(doc) + '\n', media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="{snapshot_id}.json"'})

    @app.get('/v1/workspaces/{workspace_id}/diagnostics', operation_id='diagnostics', description='Read local workspace readiness and known limitations. PUBLIC_CALIBRATION_GAP remains explicit; model configured does not mean validated accuracy.', response_model=m.Envelope[m.Diagnostics])
    def diagnostics(workspace_id: m.Token, request: Request):
        return envelope(request, service().diagnostics(workspace_id))

    return app
