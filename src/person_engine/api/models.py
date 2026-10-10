"""HTTP DTOs; business validation remains in the frozen Public V0 core."""
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field

Token = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
Sha = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Revision = Annotated[int, Field(strict=True, ge=0)]
Timestamp = Annotated[str, Field(json_schema_extra={'format': 'date-time'}, description='UTC RFC 3339 timestamp with timezone.')]


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Basis(Model):
    workspace_id: Token
    revision: Revision
    source_digest: Sha


class WorkspaceCreate(Model):
    workspace_key: Token


class WorkspaceView(Model):
    workspace_id: Token
    created_at: Timestamp
    state: Literal['INITIALIZED'] = 'INITIALIZED'
    contract: str
    api_version: Literal['v1'] = 'v1'
    basis: Basis
    source_directory: str = Field(description='Opaque import directory relative to the operator service root; needed for out-of-band local file placement. Do not parse its layout.')


class AssetCreate(Model):
    source_namespace: Token
    source_ref: Token
    source_path: Annotated[str, Field(min_length=1, max_length=512)]
    content_sha256: Sha
    bytes: Annotated[int, Field(strict=True, gt=0, le=67108864)]
    media_type: Literal['image/jpeg', 'image/png']
    expected_revision: Revision


class RegistrationReceipt(Model):
    contract: str
    workspace_id: Token
    asset_id: Token
    content_sha256: Sha
    registration_id: Token
    registry_revision: Revision
    action: Literal['REGISTERED', 'BOUND_REPLAY']
    receipt_digest: Sha


class RegistrationView(Model):
    asset_id: Token
    status: Literal['CREATED', 'REPLAY']
    replay: bool
    receipt: RegistrationReceipt


class Policy(Model):
    match_similarity: Annotated[float, Field(strict=True, ge=-1, le=1)]
    match_margin: Annotated[float, Field(strict=True, ge=0, le=2)]
    cluster_similarity: Annotated[float, Field(strict=True, ge=-1, le=1)]


class RunCreate(Model):
    asset_ids: Annotated[list[Token], Field(max_length=512)]
    expected_basis: Basis
    policy: Policy | None = None


class ReviewCreate(Model):
    run_id: Token


class HumanState(Model):
    state: Literal['UNKNOWN', 'ASSIGNED', 'OUTSIDER', 'IGNORED', 'UNASSIGNED']
    person_id: Token | None


class DecisionBase(Model):
    face_id: Token
    expected_state: HumanState
    enroll_reference: Annotated[bool, Field(strict=True)] = False


class ExistingDecision(DecisionBase):
    action: Literal['EXISTING_PERSON', 'REJECT']
    person_id: Token


class NewDecision(DecisionBase):
    action: Literal['NEW_PERSON']
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    person_key: Token


class StateDecision(DecisionBase):
    action: Literal['OUTSIDER', 'IGNORE_FACE', 'UNASSIGN']


class CorrectionDecision(DecisionBase):
    action: Literal['CORRECTION']
    to: HumanState


Decision = Annotated[ExistingDecision | NewDecision | StateDecision | CorrectionDecision, Field(discriminator='action')]


class DecisionSave(Model):
    revision: Revision
    decisions: Annotated[list[Decision], Field(min_length=1, max_length=512)]


class ApplyRequest(Model):
    revision: Revision


class SnapshotCreate(Model):
    expected_basis: Basis


class Person(Model):
    person_id: Token
    display_name: str
    status: str


class Geometry(Model):
    bbox: list[float]
    landmarks: list[list[float]]
    score: float


class Face(HumanState):
    face_id: Token
    asset_id: Token
    detection_id: Token
    model_space: Sha
    row_index: int
    geometry: Geometry


class Assignment(Model):
    face_id: Token
    person_id: Token
    event_id: Token


class CandidateScore(Model):
    person_id: Token
    similarity: float = Field(description='Uncalibrated cosine similarity; ranking evidence, not identity confidence.')


class ProvisionalGroup(Model):
    cluster_id: Token
    face_ids: list[Token]
    kind: Literal['PROVISIONAL_UNKNOWN_GROUP']
    person_id: None


class Candidate(Face):
    candidate_person_id: Token | None
    scores: list[CandidateScore]
    comparison: str
    authority: Literal['NON_AUTHORITATIVE']
    rejected_person_ids: list[Token]


class MachineResult(Model):
    run_id: Token
    basis: Basis
    policy: Policy
    candidates: list[Candidate]
    groups: list[ProvisionalGroup]
    roster_person_ids: list[Token]
    calibration_status: str
    identity_authority: str
    model_spaces: list[Sha]
    result_digest: Sha


class ApplyReceipt(Model):
    session_id: Token = Field(description='Review resource ID; identical to the review_id URL parameter. Core receipt naming is preserved.')
    revision: Revision
    decision_digest: Sha
    application_id: Token
    domain_revision: Revision
    event_ids: list[Token]
    new_persons: dict[str, Token]
    applied_at: Timestamp
    receipt_digest: Sha


class ReviewView(Model):
    session_id: Token = Field(description='Review resource ID; identical to the review_id URL parameter. Core receipt naming is preserved.')
    revision: Revision
    status: Literal['DRAFT', 'APPLIED']
    basis: Basis
    source: MachineResult
    decisions: list[Decision]
    applied_receipt: ApplyReceipt | None


class DecisionReceipt(Model):
    session_id: Token = Field(description='Review resource ID; identical to the review_id URL parameter. Core receipt naming is preserved.')
    revision: Revision
    decision_digest: Sha


class SnapshotReceipt(Model):
    workspace_id: Token
    snapshot_id: Token
    snapshot_version: int
    schema_version: str
    source_digest: Sha
    content_digest: Sha
    payload_sha256: Sha
    published_at: Timestamp


class SnapshotDocument(Model):
    """Public V0 top-level contract; opaque provenance is passed through unchanged."""
    workspace_id: Token
    snapshot_id: Token
    snapshot_version: int
    schema_version: str
    producer_version: str
    persons: list[Person]
    faces: list[Face]
    assignments: list[Assignment]
    ignored_state: list[dict]
    provenance: dict
    source_digest: Sha
    generated_at: Timestamp
    compatibility: dict


class ErrorView(Model):
    code: str
    message: str
    request_id: str
    details: dict | None = None


class JobView(Model):
    job_id: Token
    workspace_id: Token
    request_id: Token
    state: Literal['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED']
    created_at: Timestamp
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    progress: Literal['queued', 'ingest', 'match', 'complete', 'stopped']
    result_refs: dict[str, str] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=lambda: ['PUBLIC_CALIBRATION_GAP'])
    error: ErrorView | None = None


class Diagnostics(Model):
    workspace_initialized: bool
    schema_version: str
    model_availability: Literal['CONFIGURED', 'MODEL_UNAVAILABLE']
    calibration_status: Literal['PUBLIC_CALIBRATION_GAP']
    latest_run_state: str | None
    snapshot_available: bool
    warnings: list[str]


class Health(Model):
    status: Literal['ok'] = 'ok'


T = TypeVar('T')


class Envelope(Model, Generic[T]):
    request_id: str
    data: T


class Page(Model, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int
