
-- 1. Users
CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Groups
CREATE TABLE groups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Group members
CREATE TABLE group_members (
    group_id BIGINT NOT NULL REFERENCES groups(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    PRIMARY KEY (group_id, user_id)
);

-- 4. Expenses
CREATE TABLE expenses (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    expense_id UUID NOT NULL,
    group_id BIGINT NOT NULL REFERENCES groups(id),
    payer_id BIGINT NOT NULL REFERENCES users(id),
    total_cents BIGINT NOT NULL CHECK (total_cents >= 0),
    description TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    idempotency_key UUID UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    voided_at TIMESTAMPTZ,
    void_reason VARCHAR(20)
    CHECK (void_reason IN ('updated', 'deleted')),

    UNIQUE (expense_id, version),
    CHECK (voided_at IS NULL OR voided_at >= created_at),
    CHECK (
    (voided_at IS NULL AND void_reason IS NULL)
    OR
    (voided_at IS NOT NULL AND void_reason IS NOT NULL)
)
);


-- Only one active version per logical expense
CREATE UNIQUE INDEX unique_active_expense
ON expenses (expense_id)
WHERE voided_at IS NULL;


-- 5. Expense shares
CREATE TABLE expense_shares (
    expense_version_id BIGINT NOT NULL REFERENCES expenses(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0),

    PRIMARY KEY (expense_version_id, user_id)
);



-- 6. Bills
CREATE TABLE bills (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id BIGINT NOT NULL REFERENCES groups(id),
    payer_id BIGINT NOT NULL REFERENCES users(id),
    tax_cents BIGINT NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
    tip_cents BIGINT NOT NULL DEFAULT 0 CHECK (tip_cents >= 0),
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'finalized')),
    expense_version_id BIGINT UNIQUE REFERENCES expenses(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 7. Bill items
CREATE TABLE bill_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id BIGINT NOT NULL REFERENCES bills(id),
    name VARCHAR(200) NOT NULL,
    price_cents BIGINT NOT NULL CHECK (price_cents >= 0)
);

-- 8. Item claims
CREATE TABLE item_claims (
    item_id BIGINT NOT NULL REFERENCES bill_items(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    PRIMARY KEY (item_id, user_id)
);



-- 9. Settlements
CREATE TABLE settlements (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    group_id BIGINT NOT NULL REFERENCES groups(id),

    from_user_id BIGINT NOT NULL REFERENCES users(id),
    to_user_id BIGINT NOT NULL REFERENCES users(id),

    amount_cents BIGINT NOT NULL
        CHECK (amount_cents > 0),

    idempotency_key UUID UNIQUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (from_user_id <> to_user_id)
);
