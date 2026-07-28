-- ---------------------------------------------------------------------------
-- Test Observability + AI Root-Cause Agent -- PostgreSQL schema
--
-- GENERATED FILE. Do not edit by hand.
--   python scripts/dump_schema.py > db/schema.sql
--
-- Applied automatically on first container start (docker-compose mounts this
-- into /docker-entrypoint-initdb.d). For an existing database, use Alembic.
-- ---------------------------------------------------------------------------


-- --------------------------------------------------------------------------
-- failure_clusters
-- --------------------------------------------------------------------------
CREATE TABLE failure_clusters (
	id VARCHAR(36) NOT NULL, 
	pattern_signature VARCHAR(64) NOT NULL, 
	representative_error TEXT, 
	root_cause VARCHAR(32), 
	confidence FLOAT, 
	occurrence_count INTEGER NOT NULL, 
	affected_tests JSONB NOT NULL, 
	affected_frameworks JSONB NOT NULL, 
	first_seen TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_seen TIMESTAMP WITH TIME ZONE NOT NULL, 
	suggested_fix TEXT, 
	is_muted BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_failure_clusters PRIMARY KEY (id), 
	CONSTRAINT ck_failure_clusters_occurrence_non_negative CHECK (occurrence_count >= 0), 
	CONSTRAINT ck_failure_clusters_root_cause_category CHECK (root_cause IN ('app_bug', 'flaky_test', 'environment', 'test_data', 'infrastructure', 'external_dependency', 'unknown'))
);

CREATE INDEX ix_failure_clusters_last_seen ON failure_clusters (last_seen);
CREATE INDEX ix_failure_clusters_occurrence_count ON failure_clusters (occurrence_count);
CREATE UNIQUE INDEX ix_failure_clusters_pattern_signature ON failure_clusters (pattern_signature);
CREATE INDEX ix_failure_clusters_root_cause ON failure_clusters (root_cause);

-- --------------------------------------------------------------------------
-- test_runs
-- --------------------------------------------------------------------------
CREATE TABLE test_runs (
	id VARCHAR(36) NOT NULL, 
	test_name VARCHAR(512) NOT NULL, 
	test_suite VARCHAR(512), 
	test_file VARCHAR(512), 
	framework VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	duration_ms INTEGER NOT NULL, 
	attempt INTEGER NOT NULL, 
	retry_count INTEGER NOT NULL, 
	error_type VARCHAR(255), 
	error_message TEXT, 
	stack_trace TEXT, 
	logs TEXT, 
	failure_signature VARCHAR(64), 
	screenshot_url VARCHAR(1024), 
	video_url VARCHAR(1024), 
	trace_url VARCHAR(1024), 
	environment VARCHAR(64) NOT NULL, 
	git_commit VARCHAR(64), 
	git_branch VARCHAR(255), 
	ci_run_id VARCHAR(255) NOT NULL, 
	ci_provider VARCHAR(64), 
	ci_job_url VARCHAR(1024), 
	worker_id VARCHAR(128), 
	cpu_percent FLOAT, 
	memory_mb FLOAT, 
	disk_io_read_mb FLOAT, 
	disk_io_write_mb FLOAT, 
	network_latency_ms FLOAT, 
	started_at TIMESTAMP WITH TIME ZONE, 
	timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	raw_payload JSONB, 
	dedupe_key VARCHAR(64), 
	CONSTRAINT pk_test_runs PRIMARY KEY (id), 
	CONSTRAINT ck_test_runs_duration_non_negative CHECK (duration_ms >= 0), 
	CONSTRAINT ck_test_runs_attempt_non_negative CHECK (attempt >= 0), 
	CONSTRAINT ck_test_runs_test_framework CHECK (framework IN ('playwright', 'cypress', 'pytest', 'selenium')), 
	CONSTRAINT ck_test_runs_test_status CHECK (status IN ('passed', 'failed', 'skipped', 'flaky', 'error')), 
	CONSTRAINT uq_test_runs_dedupe_key UNIQUE (dedupe_key)
);

CREATE INDEX ix_test_runs_ci_run_framework ON test_runs (ci_run_id, framework);
CREATE INDEX ix_test_runs_ci_run_id ON test_runs (ci_run_id);
CREATE INDEX ix_test_runs_environment ON test_runs (environment);
CREATE INDEX ix_test_runs_error_type ON test_runs (error_type);
CREATE INDEX ix_test_runs_failure_signature ON test_runs (failure_signature);
CREATE INDEX ix_test_runs_framework ON test_runs (framework);
CREATE INDEX ix_test_runs_git_commit ON test_runs (git_commit);
CREATE INDEX ix_test_runs_name_timestamp ON test_runs (test_name, timestamp);
CREATE INDEX ix_test_runs_status ON test_runs (status);
CREATE INDEX ix_test_runs_status_timestamp ON test_runs (status, timestamp);
CREATE INDEX ix_test_runs_test_name ON test_runs (test_name);
CREATE INDEX ix_test_runs_test_suite ON test_runs (test_suite);
CREATE INDEX ix_test_runs_timestamp ON test_runs (timestamp);

-- --------------------------------------------------------------------------
-- failure_analyses
-- --------------------------------------------------------------------------
CREATE TABLE failure_analyses (
	id VARCHAR(36) NOT NULL, 
	test_result_id VARCHAR(36) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	root_cause VARCHAR(32), 
	confidence_score FLOAT, 
	reasoning TEXT, 
	key_evidence JSONB NOT NULL, 
	suggestions JSONB NOT NULL, 
	requires_human_review BOOLEAN NOT NULL, 
	error_message TEXT, 
	model VARCHAR(128), 
	prompt_version VARCHAR(32), 
	iterations INTEGER NOT NULL, 
	input_tokens INTEGER, 
	output_tokens INTEGER, 
	latency_ms INTEGER, 
	cluster_id VARCHAR(36), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_failure_analyses PRIMARY KEY (id), 
	CONSTRAINT ck_failure_analyses_confidence_in_range CHECK (confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)), 
	CONSTRAINT fk_failure_analyses_test_result_id_test_runs FOREIGN KEY(test_result_id) REFERENCES test_runs (id) ON DELETE CASCADE, 
	CONSTRAINT ck_failure_analyses_analysis_status CHECK (status IN ('pending', 'completed', 'failed')), 
	CONSTRAINT ck_failure_analyses_root_cause_category CHECK (root_cause IN ('app_bug', 'flaky_test', 'environment', 'test_data', 'infrastructure', 'external_dependency', 'unknown')), 
	CONSTRAINT fk_failure_analyses_cluster_id_failure_clusters FOREIGN KEY(cluster_id) REFERENCES failure_clusters (id) ON DELETE SET NULL
);

CREATE INDEX ix_failure_analyses_cluster_id ON failure_analyses (cluster_id);
CREATE INDEX ix_failure_analyses_created_at ON failure_analyses (created_at);
CREATE INDEX ix_failure_analyses_prompt_version ON failure_analyses (prompt_version);
CREATE INDEX ix_failure_analyses_requires_human_review ON failure_analyses (requires_human_review);
CREATE INDEX ix_failure_analyses_root_cause ON failure_analyses (root_cause);
CREATE INDEX ix_failure_analyses_root_cause_created ON failure_analyses (root_cause, created_at);
CREATE INDEX ix_failure_analyses_status ON failure_analyses (status);
CREATE INDEX ix_failure_analyses_test_result_id ON failure_analyses (test_result_id);

-- --------------------------------------------------------------------------
-- feedback
-- --------------------------------------------------------------------------
CREATE TABLE feedback (
	id VARCHAR(36) NOT NULL, 
	analysis_id VARCHAR(36) NOT NULL, 
	verdict VARCHAR(32) NOT NULL, 
	corrected_root_cause VARCHAR(32), 
	feedback_text TEXT, 
	submitted_by VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_feedback PRIMARY KEY (id), 
	CONSTRAINT analysis_id_submitted_by UNIQUE (analysis_id, submitted_by), 
	CONSTRAINT fk_feedback_analysis_id_failure_analyses FOREIGN KEY(analysis_id) REFERENCES failure_analyses (id) ON DELETE CASCADE, 
	CONSTRAINT ck_feedback_feedback_verdict CHECK (verdict IN ('correct', 'incorrect', 'uncertain')), 
	CONSTRAINT ck_feedback_root_cause_category CHECK (corrected_root_cause IN ('app_bug', 'flaky_test', 'environment', 'test_data', 'infrastructure', 'external_dependency', 'unknown'))
);

CREATE INDEX ix_feedback_analysis_id ON feedback (analysis_id);
CREATE INDEX ix_feedback_created_at ON feedback (created_at);
CREATE INDEX ix_feedback_verdict ON feedback (verdict);
