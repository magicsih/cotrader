-- +goose Up
-- +goose StatementBegin
CREATE TABLE ladders (
    id           CHAR(36)       NOT NULL PRIMARY KEY,
    venue        VARCHAR(16)    NOT NULL,
    symbol       VARCHAR(32)    NOT NULL,
    side         VARCHAR(8)     NOT NULL,
    basis        VARCHAR(16)    NOT NULL DEFAULT 'quote',
    base_price   DECIMAL(36,18) NOT NULL DEFAULT 0,
    start_pct    DECIMAL(18,8)  NOT NULL DEFAULT 0,
    end_pct      DECIMAL(18,8)  NOT NULL DEFAULT 0,
    rungs        INT            NOT NULL DEFAULT 0,
    total        DECIMAL(36,18) NOT NULL DEFAULT 0,
    status       VARCHAR(16)    NOT NULL,
    message_id   BIGINT         NULL,
    entry_field  VARCHAR(24)    NULL,
    entry_value  VARCHAR(40)    NULL,
    reason       VARCHAR(255)   NULL,
    created_at   DATETIME(6)    NOT NULL,
    updated_at   DATETIME(6)    NOT NULL,
    INDEX ix_ladders_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE TABLE ladder_orders (
    id                CHAR(36)       NOT NULL PRIMARY KEY,
    ladder_id         CHAR(36)       NOT NULL,
    rung              INT            NOT NULL,
    price             DECIMAL(36,18) NOT NULL,
    quantity          DECIMAL(36,18) NOT NULL,
    status            VARCHAR(16)    NOT NULL,
    broker_id         VARCHAR(64)    NULL,
    submitted_at      DATETIME(6)    NULL,
    filled_quantity   DECIMAL(36,18) NOT NULL DEFAULT 0,
    filled_amount     DECIMAL(36,18) NOT NULL DEFAULT 0,
    costs             DECIMAL(36,18) NOT NULL DEFAULT 0,
    costs_final       TINYINT(1)     NOT NULL DEFAULT 0,
    notified_quantity DECIMAL(36,18) NOT NULL DEFAULT 0,
    reason            VARCHAR(255)   NULL,
    created_at        DATETIME(6)    NOT NULL,
    updated_at        DATETIME(6)    NOT NULL,
    UNIQUE KEY uq_ladder_orders_broker (broker_id),
    UNIQUE KEY uq_ladder_orders_rung (ladder_id, rung),
    INDEX ix_ladder_orders_status (status),
    CONSTRAINT fk_ladder_orders_ladder FOREIGN KEY (ladder_id) REFERENCES ladders (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE TABLE transfers (
    id         CHAR(36)       NOT NULL PRIMARY KEY,
    direction  VARCHAR(16)    NOT NULL,
    currency   VARCHAR(20)    NOT NULL,
    amount     DECIMAL(36,18) NOT NULL,
    identifier VARCHAR(64)    NOT NULL,
    status     VARCHAR(16)    NOT NULL,
    reason     VARCHAR(255)   NULL,
    created_at DATETIME(6)    NOT NULL,
    updated_at DATETIME(6)    NOT NULL,
    UNIQUE KEY uq_transfers_identifier (identifier)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE TABLE events (
    id         CHAR(36)    NOT NULL PRIMARY KEY,
    kind       VARCHAR(32) NOT NULL,
    ladder_id  CHAR(36)    NULL,
    message    TEXT        NOT NULL,
    notify     TINYINT(1)  NOT NULL DEFAULT 0,
    sent       TINYINT(1)  NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL,
    INDEX ix_events_queue (notify, sent, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- +goose StatementEnd

-- +goose StatementBegin
CREATE TABLE runtime_state (
    name       VARCHAR(64) NOT NULL PRIMARY KEY,
    data       JSON        NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DROP TABLE IF EXISTS ladder_orders;
-- +goose StatementEnd
-- +goose StatementBegin
DROP TABLE IF EXISTS ladders;
-- +goose StatementEnd
-- +goose StatementBegin
DROP TABLE IF EXISTS transfers;
-- +goose StatementEnd
-- +goose StatementBegin
DROP TABLE IF EXISTS events;
-- +goose StatementEnd
-- +goose StatementBegin
DROP TABLE IF EXISTS runtime_state;
-- +goose StatementEnd
