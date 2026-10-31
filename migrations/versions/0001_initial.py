"""Initial MVP schema."""

from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("sources", Column("id", Integer, primary_key=True), Column("name", String(300), nullable=False), Column("source_type", String(20), nullable=False), Column("canonical_url", String(2048), nullable=False, unique=True), Column("category", String(100)), Column("trust_level", String(30)), Column("lifecycle_state", String(20), nullable=False), Column("enabled", Boolean, nullable=False), Column("approved_by", Integer), Column("rejection_reason", Text), Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False), Column("last_checked_at", String(40)), CheckConstraint("source_type IN ('rss', 'webpage')", name="ck_source_type"), CheckConstraint("lifecycle_state IN ('discovered','quarantined','validated','approved','active','rejected','disabled')", name="ck_source_state"), CheckConstraint("lifecycle_state != 'rejected' OR rejection_reason IS NOT NULL", name="ck_source_rejection_reason"), CheckConstraint("enabled = 0 OR lifecycle_state = 'active'", name="ck_source_enabled_active"))
    op.create_index("ix_sources_state_enabled", "sources", ["lifecycle_state", "enabled"])
    op.create_table("articles", Column("id", Integer, primary_key=True), Column("source_id", Integer, ForeignKey("sources.id", ondelete="RESTRICT")), Column("original_url", String(2048), nullable=False), Column("canonical_url", String(2048), nullable=False, unique=True), Column("content_hash", String(64), nullable=False, unique=True), Column("ingest_kind", String(20), nullable=False), Column("title", String(1000)), Column("author", String(500)), Column("published_at", String(40)), Column("discovered_at", String(40), nullable=False), Column("status", String(30), nullable=False), Column("rejection_reason", Text), Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False), CheckConstraint("status IN ('discovered','extracted','processed','completed','rejected','failed','duplicate')", name="ck_article_status"))
    op.create_index("ix_articles_status_discovered", "articles", ["status", "discovered_at"])
    op.create_table("article_text", Column("article_id", Integer, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True), Column("extracted_text", Text, nullable=False), Column("stored_at", String(40), nullable=False), Column("expires_at", String(40), nullable=False))
    op.create_index("ix_article_text_expires", "article_text", ["expires_at"])
    op.create_table("drafts", Column("id", Integer, primary_key=True), Column("article_id", Integer, ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False), Column("generated_text", Text, nullable=False), Column("edited_text", Text), Column("category", String(100), nullable=False), Column("generation_model", String(200), nullable=False), Column("content_hash", String(64), nullable=False), Column("draft_state", String(30), nullable=False), Column("approved_by", Integer), Column("approved_at", String(40)), Column("rejected_by", Integer), Column("rejected_at", String(40)), Column("rejection_reason", Text), Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False), Column("scheduled_at", String(40)), CheckConstraint("draft_state IN ('generated','pending_approval','approved','scheduled','publishing','published','rejected','cancelled')", name="ck_draft_state"), CheckConstraint("draft_state NOT IN ('approved','scheduled','publishing','published') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)", name="ck_draft_approval_record"), CheckConstraint("draft_state != 'rejected' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL AND rejection_reason IS NOT NULL)", name="ck_draft_rejection_record"), UniqueConstraint("article_id", "content_hash", name="uq_draft_article_hash"))
    op.create_index("ix_drafts_state_created", "drafts", ["draft_state", "created_at"])
    op.create_table("publication_attempts", Column("id", Integer, primary_key=True), Column("draft_id", Integer, ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False), Column("destination_id", String(200), nullable=False), Column("attempt_number", Integer, nullable=False), Column("state", String(20), nullable=False), Column("started_at", String(40), nullable=False), Column("completed_at", String(40)), Column("telegram_message_id", String(100)), Column("error_code", String(100)), Column("error_summary", String(500)), CheckConstraint("state IN ('sending','succeeded','failed','unknown')", name="ck_attempt_state"), CheckConstraint("state != 'succeeded' OR (completed_at IS NOT NULL AND telegram_message_id IS NOT NULL)", name="ck_attempt_success_record"), UniqueConstraint("draft_id", "destination_id", "attempt_number", name="uq_attempt_sequence"))
    op.create_index("ix_attempt_state_started", "publication_attempts", ["state", "started_at"])
    op.create_index("ix_attempt_draft_destination", "publication_attempts", ["draft_id", "destination_id"])
    op.create_table("publications", Column("id", Integer, primary_key=True), Column("draft_id", Integer, ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False), Column("destination_id", String(200), nullable=False), Column("telegram_message_id", String(100), nullable=False), Column("published_at", String(40), nullable=False), UniqueConstraint("draft_id", "destination_id", name="uq_publication_draft_destination"), UniqueConstraint("destination_id", "telegram_message_id", name="uq_publication_message"))
    op.create_index("ix_publications_published_at", "publications", ["published_at"])
    op.create_table("audit_events", Column("id", Integer, primary_key=True), Column("event_type", String(100), nullable=False), Column("occurred_at", String(40), nullable=False), Column("actor_type", String(30), nullable=False), Column("actor_id", String(200)), Column("target_type", String(50), nullable=False), Column("target_id", String(200)), Column("details", Text, nullable=False))
    op.create_index("ix_audit_target", "audit_events", ["target_type", "target_id"])
    op.create_index("ix_audit_time", "audit_events", ["occurred_at"])
    op.create_table("quota_usage", Column("id", Integer, primary_key=True), Column("provider", String(40), nullable=False), Column("model", String(200), nullable=False), Column("usage_date", String(10), nullable=False), Column("request_count", Integer, nullable=False), Column("reserved_count", Integer, nullable=False), Column("reserved_token_count", Integer, nullable=False), Column("token_count", Integer), Column("usage_uncertain", Boolean, nullable=False), Column("updated_at", String(40), nullable=False), CheckConstraint("request_count >= 0", name="ck_quota_requests_nonnegative"), CheckConstraint("reserved_count >= 0", name="ck_quota_reserved_nonnegative"), CheckConstraint("reserved_token_count >= 0", name="ck_quota_reserved_tokens_nonnegative"), UniqueConstraint("provider", "model", "usage_date", name="uq_quota_period"))
    op.create_index("ix_quota_usage_date", "quota_usage", ["usage_date"])
    op.create_table("app_settings", Column("key", String(100), primary_key=True), Column("value", String(500), nullable=False), Column("updated_at", String(40), nullable=False))


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_quota_usage_date", table_name="quota_usage")
    op.drop_table("quota_usage")
    op.drop_index("ix_audit_time", table_name="audit_events")
    op.drop_index("ix_audit_target", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_publications_published_at", table_name="publications")
    op.drop_table("publications")
    op.drop_index("ix_attempt_draft_destination", table_name="publication_attempts")
    op.drop_index("ix_attempt_state_started", table_name="publication_attempts")
    op.drop_table("publication_attempts")
    op.drop_index("ix_drafts_state_created", table_name="drafts")
    op.drop_table("drafts")
    op.drop_index("ix_article_text_expires", table_name="article_text")
    op.drop_table("article_text")
    op.drop_index("ix_articles_status_discovered", table_name="articles")
    op.drop_table("articles")
    op.drop_index("ix_sources_state_enabled", table_name="sources")
    op.drop_table("sources")
