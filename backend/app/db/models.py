from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

JSONType = JSON().with_variant(JSONB, "postgresql")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_url: Mapped[str] = mapped_column(String(500), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    analysis = relationship("Analysis", back_populates="project", uselist=False, cascade="all, delete-orphan")
    deployments = relationship("Deployment", back_populates="project", cascade="all, delete-orphan")
    pipeline_steps = relationship("PipelineStep", back_populates="project", cascade="all, delete-orphan")
    generated_files = relationship("GeneratedFile", back_populates="project", cascade="all, delete-orphan")
    monitoring_events = relationship("MonitoringEvent", back_populates="project", cascade="all, delete-orphan")
    monitoring_alerts = relationship("MonitoringAlert", back_populates="project", cascade="all, delete-orphan")
    autoscaling_events = relationship("AutoscalingEvent", back_populates="project", cascade="all, delete-orphan")



class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True)
    language: Mapped[str | None] = mapped_column(String(100))
    framework: Mapped[str | None] = mapped_column(String(100))
    package_manager: Mapped[str | None] = mapped_column(String(100))
    entrypoint: Mapped[str | None] = mapped_column(String(255))
    test_command: Mapped[str | None] = mapped_column(String(255))
    has_dockerfile: Mapped[bool] = mapped_column(default=False)
    security_score: Mapped[int | None] = mapped_column(Integer)
    analysis_result: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="analysis")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    environment: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    deployment_type: Mapped[str] = mapped_column(String(50), default="standard")
    details: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="deployments")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    details: Mapped[dict | None] = mapped_column(JSONType)


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project = relationship("Project", back_populates="pipeline_steps")


class GeneratedFile(Base):
    __tablename__ = "generated_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="generated_files")


class MonitoringEvent(Base):
    __tablename__ = "monitoring_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    deployment_id: Mapped[int | None] = mapped_column(ForeignKey("deployments.id", ondelete="SET NULL"), nullable=True, index=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True, index=True)
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    workflow_context: Mapped[str] = mapped_column(String(30), default="idle", nullable=False)
    processing_status: Mapped[str] = mapped_column(String(30), default="recorded", nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="monitoring_events")


class MonitoringAlert(Base):
    __tablename__ = "monitoring_alerts"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_monitoring_alert_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    deployment_id: Mapped[int | None] = mapped_column(ForeignKey("deployments.id", ondelete="SET NULL"), nullable=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project = relationship("Project", back_populates="monitoring_alerts")


class AutoscalingEvent(Base):
    __tablename__ = "autoscaling_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    current_replicas: Mapped[int | None] = mapped_column(Integer)
    desired_replicas: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSONType)

    project = relationship("Project", back_populates="autoscaling_events")
    deployment = relationship("Deployment")


class ReleaseApproval(Base):
    __tablename__ = "release_approvals"
    __table_args__ = (UniqueConstraint("staging_deployment_id", name="uq_release_approval_staging_deployment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    staging_deployment_id: Mapped[int | None] = mapped_column(ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=True, index=True)
    production_deployment_id: Mapped[int | None] = mapped_column(ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="Draft", index=True)
    requested_by: Mapped[str | None] = mapped_column(String(255))
    requester_identity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approver_identity: Mapped[str | None] = mapped_column(String(255))
    approver_identity_verified: Mapped[bool | None] = mapped_column(Boolean)
    decision: Mapped[str | None] = mapped_column(String(20))
    decision_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comments: Mapped[str | None] = mapped_column(Text)
    release_version: Mapped[str | None] = mapped_column(String(255))
    branch: Mapped[str | None] = mapped_column(String(255))
    commit_id: Mapped[str | None] = mapped_column(String(100))
    commit_message: Mapped[str | None] = mapped_column(String(1000))
    release_summary: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReleaseApprovalEvent(Base):
    __tablename__ = "release_approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("release_approvals.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_identity_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    comments: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONType)

