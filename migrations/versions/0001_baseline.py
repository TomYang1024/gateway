"""Frozen V1.3 PostgreSQL baseline. Future changes require an incremental revision."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = [
    "CREATE TABLE admin_audit_logs (\n\tid VARCHAR NOT NULL, \n\tactor VARCHAR NOT NULL, \n\trole VARCHAR NOT NULL, \n\taction VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE budget_periods (\n\tid VARCHAR NOT NULL, \n\tteam_id VARCHAR NOT NULL, \n\tperiod VARCHAR NOT NULL, \n\tamount NUMERIC(30, 12) NOT NULL, \n\tcomplete BOOLEAN NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE call_settlements (\n\tcall_id VARCHAR NOT NULL, \n\tPRIMARY KEY (call_id)\n)",
    "CREATE TABLE cost_adjustments (\n\tid VARCHAR NOT NULL, \n\tteam_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE llm_attempts (\n\tid VARCHAR NOT NULL, \n\tcall_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_llm_attempts_call_id ON llm_attempts (call_id)",
    "CREATE TABLE llm_call_events (\n\tid VARCHAR NOT NULL, \n\tcall_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_llm_call_events_call_id ON llm_call_events (call_id)",
    "CREATE TABLE llm_calls (\n\tid VARCHAR NOT NULL, \n\tteam_id VARCHAR NOT NULL, \n\tapplication_id VARCHAR NOT NULL, \n\tlogical_model VARCHAR NOT NULL, \n\tbusiness_ref VARCHAR, \n\tstatus VARCHAR NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_llm_calls_created_at ON llm_calls (created_at)",
    "CREATE INDEX ix_llm_calls_team_id ON llm_calls (team_id)",
    "CREATE INDEX ix_llm_calls_application_id ON llm_calls (application_id)",
    "CREATE INDEX ix_llm_calls_logical_model ON llm_calls (logical_model)",
    "CREATE INDEX ix_llm_calls_status ON llm_calls (status)",
    "CREATE INDEX ix_llm_calls_business_ref ON llm_calls (business_ref)",
    "CREATE TABLE llm_content_refs (\n\tid VARCHAR NOT NULL, \n\tcall_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_llm_content_refs_call_id ON llm_content_refs (call_id)",
    "CREATE TABLE llm_route_decisions (\n\tid VARCHAR NOT NULL, \n\tcall_id VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_llm_route_decisions_call_id ON llm_route_decisions (call_id)",
    "CREATE TABLE notification_outbox (\n\tid VARCHAR NOT NULL, \n\tdedup_key VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at TIMESTAMP WITH TIME ZONE, \n\tlast_error VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (dedup_key)\n)",
    "CREATE INDEX ix_notification_outbox_status ON notification_outbox (status)",
    "CREATE TABLE policy_head (\n\tid SERIAL NOT NULL, \n\tversion INTEGER NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE policy_versions (\n\tversion SERIAL NOT NULL, \n\tsnapshot JSON NOT NULL, \n\treason TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (version)\n)",
    "CREATE TABLE prompt_refresh_jobs (\n\tid VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (id)\n)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Restore a verified backup; downgrade would discard the ledger")
