-- 智能投资助手 schema v1（任务 1.2/1.3）
-- 规格书 §3.3 全部表（22 行清单/27 张）+ trade_record（R18.1 留痕补充）= 28 张。
-- ★ append-only 表 + 加严的 ruling/veto_verdict/trade_record：触发器禁 UPDATE/DELETE。
-- ☆ 纳入快照范围的表由 snapshot_member 关联表达。

BEGIN;

-- ============ 基础与配置 ============

CREATE TABLE IF NOT EXISTS instrument (           -- ☆
    symbol        text PRIMARY KEY,
    market        text NOT NULL,
    name          text NOT NULL DEFAULT '',
    market_cap    numeric,
    small_cap     boolean,
    scope_group   text NOT NULL CHECK (scope_group IN ('A','B','C')),
    chain_role    text CHECK (chain_role IN ('shovel','miner')),   -- 卖铲子/淘金者（D6 季度更新）
    state         text NOT NULL DEFAULT 'candidate'
                  CHECK (state IN ('candidate','holding','eliminated','stranded',
                                   'observation','disrupted','deferred')),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source (
    source_id     text PRIMARY KEY,
    publisher     text NOT NULL,
    domains       text[] NOT NULL DEFAULT '{}',
    tier          text NOT NULL CHECK (tier IN ('official','authoritative','secondary'))
);

CREATE TABLE IF NOT EXISTS source_role (          -- 主/备/仲裁角色是数据（R1.4）
    domain        text NOT NULL,                  -- D1..D10
    market        text NOT NULL,
    role          text NOT NULL CHECK (role IN ('primary','backup','arbiter')),
    adapter       text NOT NULL,
    valid_from    date NOT NULL,
    valid_to      date,
    PRIMARY KEY (domain, market, role, valid_from)
);

CREATE TABLE IF NOT EXISTS field_dict (           -- 字段字典（R2.2）
    field_name    text NOT NULL,
    scenario      text NOT NULL DEFAULT '*',
    conservative  text NOT NULL CHECK (conservative IN ('higher','lower')),
    tolerance     numeric,
    PRIMARY KEY (field_name, scenario)
);

-- ============ 证据与缺口 ============

CREATE TABLE IF NOT EXISTS evidence (             -- ★☆
    evidence_id   text PRIMARY KEY,
    symbol        text,
    field_name    text NOT NULL,
    value_json    jsonb NOT NULL,
    unit          text NOT NULL DEFAULT '',
    source_id     text REFERENCES source(source_id),
    as_of         date,
    collected_at  timestamptz NOT NULL DEFAULT now(),
    freshness     text NOT NULL DEFAULT 'fresh' CHECK (freshness IN ('fresh','stale')),
    confidence    text NOT NULL DEFAULT 'unverified'
                  CHECK (confidence IN ('verified','unverified')),   -- 仅校验器置位（A1）
    approx        boolean NOT NULL DEFAULT false,
    agent_task_id text,
    superseded_by text REFERENCES evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS corroboration (
    symbol        text NOT NULL,
    field_name    text NOT NULL,
    period        text NOT NULL,
    evidence_ids  text[] NOT NULL,
    independent   boolean NOT NULL,                -- 发布主体不同且非同一原始通稿
    PRIMARY KEY (symbol, field_name, period)
);

CREATE TABLE IF NOT EXISTS gap (                  -- ★
    gap_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    field_name    text NOT NULL,
    subject       text NOT NULL,
    attempted     text[] NOT NULL DEFAULT '{}',
    degradation   text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ============ 规则与参数 ============

CREATE TABLE IF NOT EXISTS rule (                 -- ☆
    rule_id       text NOT NULL,
    version       text NOT NULL,
    source_ref    text NOT NULL,                   -- 框架出处
    rule_type     text NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}',
    effect_level  int  NOT NULL,
    conservative  text NOT NULL DEFAULT '',
    PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS param_signed (         -- ☆
    name          text NOT NULL,
    version       int  NOT NULL,
    value         numeric NOT NULL,
    direction     text NOT NULL CHECK (direction IN ('up_only','down_only','fixed')),
    signed_at     date NOT NULL,
    note          text NOT NULL DEFAULT '',
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS param_override (       -- 生效覆盖参数（R14.6/14.8/15.2）
    name          text NOT NULL,
    value         numeric NOT NULL,
    valid_from    date NOT NULL,
    valid_to      date,
    origin_rule   text NOT NULL,
    PRIMARY KEY (name, valid_from, origin_rule)
);

CREATE TABLE IF NOT EXISTS ruling (               -- ☆（加严 append-only，R18.1 留痕）
    ruling_id     text PRIMARY KEY,
    issue         text NOT NULL,
    decision      text NOT NULL,
    decided_at    date,
    affected      text[] NOT NULL DEFAULT '{}',
    review_cond   text NOT NULL DEFAULT '',
    superseded_by text REFERENCES ruling(ruling_id)
);

-- ============ 快照 ============

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id   text PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('monthly','event')),
    cutoff        date NOT NULL,
    rule_version  text NOT NULL,
    param_version text NOT NULL,
    rulings       text[] NOT NULL DEFAULT '{}',
    content_hash  text NOT NULL DEFAULT '',
    frozen        boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS snapshot_member (
    snapshot_id   text NOT NULL REFERENCES snapshot(snapshot_id),
    evidence_id   text NOT NULL REFERENCES evidence(evidence_id),
    PRIMARY KEY (snapshot_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_snapmember_ev ON snapshot_member (evidence_id);

-- ============ 代理任务 ============

CREATE TABLE IF NOT EXISTS agent_task (
    symbol        text NOT NULL DEFAULT '',
    task_type     text NOT NULL,
    sub_item      text NOT NULL DEFAULT '',
    period        text NOT NULL,
    template_ver  text NOT NULL,
    attempt       int  NOT NULL DEFAULT 1,
    status        text NOT NULL CHECK (status IN ('queued','running','ok','stranded','void')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, task_type, sub_item, period, template_ver, attempt)  -- 自然键幂等（R16.4）
);

-- ============ 论点卡 / 决策 / 签认 / 意见 ============

CREATE TABLE IF NOT EXISTS thesis_card (          -- ★☆
    card_id       text PRIMARY KEY,
    symbol        text NOT NULL,
    logic_line    text NOT NULL,
    assumptions   jsonb NOT NULL,                   -- 3-5 条，各含数字/时间/增强/转弱判据/核心标记
    scenarios     jsonb NOT NULL,
    falsify       jsonb NOT NULL,
    sell_plan     text NOT NULL DEFAULT '',
    opinion_refs  text[] NOT NULL DEFAULT '{}',
    mark_history  jsonb NOT NULL DEFAULT '[]',      -- 季度 ✅/⚠️/❌ 历史
    warn_streak   int NOT NULL DEFAULT 0,
    superseded_by text REFERENCES thesis_card(card_id)
);

CREATE TABLE IF NOT EXISTS decision_record (      -- ★
    record_id     text PRIMARY KEY,
    symbol        text NOT NULL,
    action        text NOT NULL,
    opinion_id    text,
    emotion_score int,
    planned       boolean,
    origin        text NOT NULL DEFAULT 'active' CHECK (origin IN ('active','framework')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    payload       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trade_record (         -- ★（规格书补充表，S9 成交回执）
    receipt_id    text PRIMARY KEY,
    record_id     text NOT NULL REFERENCES decision_record(record_id),
    symbol        text NOT NULL,
    executed_at   date NOT NULL,
    quantity      numeric NOT NULL,
    price         numeric NOT NULL,
    entered_by    text NOT NULL DEFAULT 'user',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signoff (              -- ★
    signoff_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_type   text NOT NULL,
    object_id     text NOT NULL,
    action        text NOT NULL CHECK (action IN ('approve','reject','modify')),
    reason        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS opinion (              -- ★
    opinion_id    text PRIMARY KEY,                 -- #YYYY-MM-NNN
    symbol        text NOT NULL,
    op_type       text NOT NULL,
    fields        jsonb NOT NULL,                   -- 10.2 十项字段（应用层 Pydantic 断言）
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','executed','expired','rejected','overdue')),
    valid_until   date,
    created_at    timestamptz NOT NULL DEFAULT now(),
    superseded_by text REFERENCES opinion(opinion_id)
);

-- ============ 市况 / 主题 / 自建资产 ============

CREATE TABLE IF NOT EXISTS market_regime (        -- ☆
    regime_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    level         text NOT NULL CHECK (level IN ('normal','overheat','mania','freeze')),
    valid_from    date NOT NULL,
    valid_to      date,
    signals_hit   text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS theme_map (            -- ☆
    theme         text NOT NULL,
    symbol        text NOT NULL,
    version       int  NOT NULL,
    valid_from    date NOT NULL,
    valid_to      date,
    PRIMARY KEY (theme, symbol, version)
);

CREATE TABLE IF NOT EXISTS guidance_track (
    symbol        text NOT NULL,
    period        text NOT NULL,
    guided        jsonb NOT NULL,
    actual        jsonb,
    hit_ratio     numeric,
    cold_start    boolean NOT NULL DEFAULT true,
    PRIMARY KEY (symbol, period)
);

CREATE TABLE IF NOT EXISTS opinion_quality (
    opinion_id    text PRIMARY KEY REFERENCES opinion(opinion_id),
    review_at     date,
    direction_ok  boolean,
    excess_return numeric,
    notes         text NOT NULL DEFAULT ''
);

-- ============ 台账 / 触发器 / 审计 ============

CREATE TABLE IF NOT EXISTS violation_ledger (     -- ★
    violation_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category      text NOT NULL,                    -- 七类事件
    subject       text NOT NULL DEFAULT '',
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    closed        boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS trigger_event (        -- ★
    event_id      text PRIMARY KEY,
    trigger_type  text NOT NULL,
    symbol        text,
    accounting_day date NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}',
    consumed_at   timestamptz,
    absorbed_by   text REFERENCES trigger_event(event_id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ============ 阶段产物（☆） ============

CREATE TABLE IF NOT EXISTS industry_score (
    period        text NOT NULL,
    industry      text NOT NULL,
    total         int  NOT NULL,
    factor_grades jsonb NOT NULL,
    qualified     boolean NOT NULL,
    pending_ruling boolean NOT NULL DEFAULT false,
    rule_ids      text[] NOT NULL,
    snapshot_id   text NOT NULL REFERENCES snapshot(snapshot_id),
    evidence_ids  text[] NOT NULL DEFAULT '{}',    -- A4 三元组
    PRIMARY KEY (period, industry)
);

CREATE TABLE IF NOT EXISTS veto_verdict (         -- 加严 append-only（R18.1 留痕）
    period        text NOT NULL,
    symbol        text NOT NULL,
    verdict       text NOT NULL CHECK (verdict IN ('pass','veto','edge','stranded')),
    items         jsonb NOT NULL,                   -- 五项条件布尔向量
    rule_ids      text[] NOT NULL,
    snapshot_id   text NOT NULL REFERENCES snapshot(snapshot_id),
    evidence_ids  text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (period, symbol)
);

CREATE TABLE IF NOT EXISTS score_card (
    period        text NOT NULL,
    symbol        text NOT NULL,
    dimensions    jsonb NOT NULL,
    total         numeric,
    pending_signoff boolean NOT NULL DEFAULT true,
    rule_ids      text[] NOT NULL,
    snapshot_id   text NOT NULL REFERENCES snapshot(snapshot_id),
    evidence_ids  text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (period, symbol)
);

CREATE TABLE IF NOT EXISTS scenario_calc (
    period        text NOT NULL,
    symbol        text NOT NULL,
    bear          numeric, base numeric, bull numeric,
    intercepted   boolean NOT NULL DEFAULT false,
    rule_ids      text[] NOT NULL,
    snapshot_id   text NOT NULL REFERENCES snapshot(snapshot_id),
    evidence_ids  text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (period, symbol)
);

CREATE TABLE IF NOT EXISTS position (
    symbol        text PRIMARY KEY,
    weight_pct    numeric NOT NULL,
    cost_basis    numeric,
    stop_level    numeric,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_task (
    task_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          text NOT NULL,
    due           date NOT NULL,
    subject       text NOT NULL DEFAULT '',
    done          boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS audit_chain (          -- ★
    day           date PRIMARY KEY,
    prev_head     text NOT NULL DEFAULT '',
    merkle_root   text NOT NULL,
    object_count  int  NOT NULL
);

-- ============ append-only 强制（任务 1.3） ============

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append-only 表 % 禁止 %（更正请追加新版本并置 superseded_by，R18.1）',
        TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'evidence','gap','thesis_card','decision_record','signoff','opinion',
        'violation_ledger','trigger_event','audit_chain',
        'ruling','veto_verdict','trade_record'          -- 加严三表（R18.1 留痕）
    ] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I;
             CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION forbid_mutation()',
            'appendonly_' || t, t, 'appendonly_' || t, t);
    END LOOP;
END $$;

-- trigger_event 的消费确认是唯一合法的"更新"，以专列白名单触发器实现：
-- 仅允许把 consumed_at 从 NULL 置为非 NULL、absorbed_by 从 NULL 置值，其余字段不可变。
CREATE OR REPLACE FUNCTION trigger_event_consume_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'trigger_event 禁止 DELETE';
    END IF;
    IF NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.trigger_type IS DISTINCT FROM OLD.trigger_type
       OR NEW.symbol IS DISTINCT FROM OLD.symbol
       OR NEW.accounting_day IS DISTINCT FROM OLD.accounting_day
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
       OR (OLD.absorbed_by IS NOT NULL AND NEW.absorbed_by IS DISTINCT FROM OLD.absorbed_by) THEN
        RAISE EXCEPTION 'trigger_event 仅允许消费确认（consumed_at/absorbed_by 单向置位）';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS appendonly_trigger_event ON trigger_event;
CREATE TRIGGER appendonly_trigger_event BEFORE UPDATE OR DELETE ON trigger_event
FOR EACH ROW EXECUTE FUNCTION trigger_event_consume_only();

-- 一跳回溯视图（R18.4）
CREATE OR REPLACE VIEW opinion_trace AS
SELECT o.opinion_id, o.symbol, o.op_type, o.status,
       o.fields->'rule_ids'      AS rule_ids,
       o.fields->>'snapshot_id'  AS snapshot_id,
       o.fields->'evidence_ids'  AS evidence_ids,
       s.action AS last_signoff_action
FROM opinion o
LEFT JOIN LATERAL (
    SELECT action FROM signoff
    WHERE object_type = 'opinion' AND object_id = o.opinion_id
    ORDER BY signoff_id DESC LIMIT 1
) s ON true;

COMMIT;
