"""SQLAlchemy persistence models."""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("source_type IN ('rss', 'webpage')", name="ck_source_type"),
        CheckConstraint("lifecycle_state IN ('discovered','quarantined','validated','approved','active','rejected','disabled')", name="ck_source_state"),
        CheckConstraint("lifecycle_state != 'rejected' OR rejection_reason IS NOT NULL", name="ck_source_rejection_reason"),
        CheckConstraint("enabled = 0 OR lifecycle_state = 'active'", name="ck_source_enabled_active"),
        Index("ix_sources_state_enabled", "lifecycle_state", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(100))
    trust_level: Mapped[str | None] = mapped_column(String(30))
    lifecycle_state: Mapped[str] = mapped_column(String(20), nullable=False, default="quarantined")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[int | None] = mapped_column(Integer)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_checked_at: Mapped[str | None] = mapped_column(String(40))


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("status IN ('discovered','extracted','processed','completed','rejected','failed','duplicate')", name="ck_article_status"),
        Index("ix_articles_status_discovered", "status", "discovered_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    ingest_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000))
    author: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[str | None] = mapped_column(String(40))
    discovered_at: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="discovered")
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    text: Mapped["ArticleText | None"] = relationship(back_populates="article", cascade="all, delete-orphan", passive_deletes=True)
    drafts: Mapped[list["Draft"]] = relationship(back_populates="article")


class ArticleText(Base):
    __tablename__ = "article_text"
    __table_args__ = (Index("ix_article_text_expires", "expires_at"),)

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    stored_at: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(40), nullable=False)
    article: Mapped[Article] = relationship(back_populates="text")


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (
        CheckConstraint("draft_state IN ('generated','pending_approval','approved','scheduled','publishing','published','rejected','cancelled')", name="ck_draft_state"),
        CheckConstraint("draft_state NOT IN ('approved','scheduled','publishing','published') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)", name="ck_draft_approval_record"),
        CheckConstraint("draft_state != 'rejected' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL AND rejection_reason IS NOT NULL)", name="ck_draft_rejection_record"),
        UniqueConstraint("article_id", "content_hash", name="uq_draft_article_hash"),
        Index("ix_drafts_state_created", "draft_state", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False)
    generated_text: Mapped[str] = mapped_column(Text, nullable=False)
    edited_text: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    generation_model: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_approval")
    approved_by: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[str | None] = mapped_column(String(40))
    rejected_by: Mapped[int | None] = mapped_column(Integer)
    rejected_at: Mapped[str | None] = mapped_column(String(40))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    scheduled_at: Mapped[str | None] = mapped_column(String(40))

    article: Mapped[Article] = relationship(back_populates="drafts")
    attempts: Mapped[list["PublicationAttempt"]] = relationship(back_populates="draft")


class PublicationAttempt(Base):
    __tablename__ = "publication_attempts"
    __table_args__ = (
        CheckConstraint("state IN ('sending','succeeded','failed','unknown')", name="ck_attempt_state"),
        CheckConstraint("state != 'succeeded' OR (completed_at IS NOT NULL AND telegram_message_id IS NOT NULL)", name="ck_attempt_success_record"),
        UniqueConstraint("draft_id", "destination_id", "attempt_number", name="uq_attempt_sequence"),
        Index("ix_attempt_state_started", "state", "started_at"),
        Index("ix_attempt_draft_destination", "draft_id", "destination_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="sending")
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(40))
    telegram_message_id: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(String(500))

    draft: Mapped[Draft] = relationship(back_populates="attempts")


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("draft_id", "destination_id", name="uq_publication_draft_destination"),
        UniqueConstraint("destination_id", "telegram_message_id", name="uq_publication_message"),
        Index("ix_publications_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    telegram_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id"), Index("ix_audit_time", "occurred_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(200))
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class QuotaUsage(Base):
    __tablename__ = "quota_usage"
    __table_args__ = (
        UniqueConstraint("provider", "model", "usage_date", name="uq_quota_period"),
        CheckConstraint("request_count >= 0", name="ck_quota_requests_nonnegative"),
        CheckConstraint("reserved_count >= 0", name="ck_quota_reserved_nonnegative"),
        CheckConstraint("reserved_token_count >= 0", name="ck_quota_reserved_tokens_nonnegative"),
        Index("ix_quota_usage_date", "usage_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    usage_date: Mapped[str] = mapped_column(String(10), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int | None] = mapped_column(Integer)
    usage_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

