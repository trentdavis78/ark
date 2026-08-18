-- BLACKTOP — Supabase (Postgres + PostGIS) schema.
--
-- Mirrors PRD §8. Every table is row-level-secured to the owning driver:
-- this database holds one person's business records, and the federated layer
-- (F14) shares only what that person explicitly marks shareable.
--
-- Compliance notes that constrain this schema:
--   I1  no column anywhere stores a platform credential, token, or cookie.
--   I5  every row is the driver's own observation of their own work.
--   F14 buildings.shareable defaults false and is only allowed to become true
--       for commercial and multi-unit properties (enforced by trigger below,
--       not by convention).

create extension if not exists postgis;
create extension if not exists pgcrypto;

-- --------------------------------------------------------------------------
-- Enums
-- --------------------------------------------------------------------------
create type platform         as enum ('doordash', 'uber_eats', 'grubhub', 'manual');
create type offer_type       as enum ('single', 'stacked', 'shop_deliver', 'large_order');
create type verdict_color    as enum ('green', 'amber', 'red', 'manual_fallback');
create type weather_bucket   as enum ('clear', 'rain', 'snow', 'severe');
create type destination_class as enum ('single_family', 'multi_unit', 'high_rise',
                                       'commercial', 'campus', 'hotel', 'unknown');
create type offer_action     as enum ('accepted', 'declined', 'expired');

-- --------------------------------------------------------------------------
-- Sessions — the mileage and earnings unit (F7)
-- --------------------------------------------------------------------------
create table sessions (
    id             uuid primary key default gen_random_uuid(),
    driver_id      uuid not null references auth.users (id) on delete cascade,
    started_at     timestamptz not null,
    ended_at       timestamptz,
    start_loc      geography(point, 4326),
    end_loc        geography(point, 4326),
    total_miles    numeric(8, 2) not null default 0 check (total_miles >= 0),
    gross_earnings numeric(10, 2) not null default 0 check (gross_earnings >= 0),
    platform_mix   jsonb not null default '{}'::jsonb,
    -- Audit-defensible log requires the purpose and the path, not just a total.
    business_purpose text not null default 'delivery driving — all online miles',
    route_polyline   text,
    created_at     timestamptz not null default now(),
    constraint session_ends_after_start check (ended_at is null or ended_at > started_at)
);
create index sessions_driver_time on sessions (driver_id, started_at desc);

-- --------------------------------------------------------------------------
-- Merchants — per *location*, never per brand (F4)
-- --------------------------------------------------------------------------
create table merchants (
    id            uuid primary key default gen_random_uuid(),
    driver_id     uuid not null references auth.users (id) on delete cascade,
    platform_name text not null,
    address       text not null,
    h3            text not null,
    geofence      geography(polygon, 4326),
    -- 168 hour-of-week buckets, 0 = Monday 00:00.
    wait_p50_by_hour numeric(5, 2)[168],
    wait_p90_by_hour numeric(5, 2)[168],
    sample_n      integer not null default 0 check (sample_n >= 0),
    created_at    timestamptz not null default now(),
    unique (driver_id, platform_name, address)
);
create index merchants_driver_h3 on merchants (driver_id, h3);

-- --------------------------------------------------------------------------
-- Buildings — the last-100-feet knowledge graph (F5), the moat
-- --------------------------------------------------------------------------
create table buildings (
    id                 uuid primary key default gen_random_uuid(),
    driver_id          uuid not null references auth.users (id) on delete cascade,
    h3_res9            text not null,
    label              text not null,
    destination_class  destination_class not null default 'unknown',
    access_notes       text,
    -- Encrypted at rest; the app holds the key, the database never sees it.
    gate_code_encrypted bytea,
    entry_door         text,
    elevator_bank      text,
    lobby_handoff_ok   boolean,
    dock_access        boolean,
    safe_park_geom     geography(point, 4326),
    friction_minutes_p50 numeric(5, 2),
    sample_n           integer not null default 0 check (sample_n >= 0),
    -- F14: opt-in only, and never a private residence.
    shareable          boolean not null default false,
    created_at         timestamptz not null default now(),
    unique (driver_id, h3_res9, label)
);
create index buildings_driver_hex on buildings (driver_id, h3_res9);

-- A single-family address must never become shareable, whatever the client
-- sends. Policy this load-bearing belongs in the database, not in a client
-- that can be out of date or wrong.
create or replace function enforce_shareable_class() returns trigger
language plpgsql as $$
begin
    if new.shareable and new.destination_class not in ('multi_unit', 'high_rise',
                                                       'commercial', 'campus', 'hotel') then
        raise exception
            'buildings.shareable is limited to commercial and multi-unit properties (got %)',
            new.destination_class;
    end if;
    if new.shareable and new.gate_code_encrypted is not null then
        raise exception 'a shared building record must not carry a gate code';
    end if;
    return new;
end $$;

create trigger buildings_shareable_guard
    before insert or update on buildings
    for each row execute function enforce_shareable_class();

-- --------------------------------------------------------------------------
-- Offers — declines are logged; they are observations, not absences
-- --------------------------------------------------------------------------
create table offers (
    id                 uuid primary key default gen_random_uuid(),
    driver_id          uuid not null references auth.users (id) on delete cascade,
    session_id         uuid references sessions (id) on delete set null,
    platform           platform not null,
    seen_at            timestamptz not null,
    displayed_payout   numeric(8, 2) not null check (displayed_payout >= 0),
    merchant_id        uuid references merchants (id) on delete set null,
    dropoff_h3         text,
    dropoff_class      destination_class not null default 'unknown',
    stated_distance_mi numeric(6, 2),
    stated_minutes     numeric(6, 2),
    offer_type         offer_type not null default 'single',
    peak_pay           numeric(6, 2) not null default 0,
    -- The highest-signal feature in the tip model, and it costs only counting.
    hit_display_cap    boolean not null default false,
    predicted_total    numeric(8, 2),
    predicted_minutes  numeric(6, 2),
    verdict            verdict_color,
    reservation_hourly numeric(8, 2),
    action_taken       offer_action,
    -- Below the confidence gate no verdict is emitted; the row still records
    -- that a card was seen and could not be read (PRD §7 telemetry).
    parse_confidence   numeric(4, 3) check (parse_confidence between 0 and 1),
    created_at         timestamptz not null default now()
);
create index offers_driver_time on offers (driver_id, seen_at desc);
create index offers_hex_hour on offers (driver_id, dropoff_h3, seen_at);
create index offers_cap_detect on offers (driver_id, displayed_payout)
    where hit_display_cap;

-- --------------------------------------------------------------------------
-- Deliveries — the free training label (F2) and the dwell sample (F4)
-- --------------------------------------------------------------------------
create table deliveries (
    id                 uuid primary key default gen_random_uuid(),
    driver_id          uuid not null references auth.users (id) on delete cascade,
    offer_id           uuid not null references offers (id) on delete cascade,
    accepted_at        timestamptz not null,
    merchant_arrive_at timestamptz,
    merchant_depart_at timestamptz,
    dropoff_at         timestamptz,
    actual_payout      numeric(8, 2) check (actual_payout >= 0),
    actual_tip         numeric(8, 2),
    actual_miles       numeric(6, 2) check (actual_miles >= 0),
    created_at         timestamptz not null default now(),
    unique (offer_id),
    constraint dwell_ordered check (
        merchant_arrive_at is null or merchant_depart_at is null
        or merchant_depart_at >= merchant_arrive_at)
);
create index deliveries_driver_time on deliveries (driver_id, accepted_at desc);

-- --------------------------------------------------------------------------
-- Zone stats — offer *quality* density, not offer density (F3)
-- --------------------------------------------------------------------------
create table zone_stats (
    driver_id           uuid not null references auth.users (id) on delete cascade,
    h3                  text not null,
    hour_of_week        smallint not null check (hour_of_week between 0 and 167),
    weather_bucket      weather_bucket not null default 'clear',
    offer_arrival_rate  numeric(8, 4) not null default 0,
    value_dist_params   jsonb not null default '{}'::jsonb,
    expected_net_hourly numeric(8, 2) not null default 0,
    sample_n            integer not null default 0,
    updated_at          timestamptz not null default now(),
    primary key (driver_id, h3, hour_of_week, weather_bucket)
);

-- --------------------------------------------------------------------------
-- Parking risk (F11) — the only table seeded from public data
-- --------------------------------------------------------------------------
create table parking_risk (
    id                  uuid primary key default gen_random_uuid(),
    geom                geography(polygon, 4326) not null,
    municipality        text not null,
    rule_type           text not null,
    active_hours        text,
    street_cleaning_cron text,
    risk_score          numeric(4, 2) not null default 0 check (risk_score between 0 and 10),
    source              text not null,
    updated_at          timestamptz not null default now()
);
create index parking_risk_geom on parking_risk using gist (geom);

-- --------------------------------------------------------------------------
-- Policy runs — the counterfactual record (F13)
-- --------------------------------------------------------------------------
create table policy_runs (
    id              uuid primary key default gen_random_uuid(),
    driver_id       uuid not null references auth.users (id) on delete cascade,
    window_start    timestamptz not null,
    window_end      timestamptz not null,
    threshold_used  numeric(6, 3) not null,
    realized_hourly numeric(8, 2) not null,
    counterfactuals jsonb not null default '[]'::jsonb,
    created_at      timestamptz not null default now(),
    constraint policy_window_ordered check (window_end > window_start)
);

-- --------------------------------------------------------------------------
-- Expenses (F7)
-- --------------------------------------------------------------------------
create table expenses (
    id                   uuid primary key default gen_random_uuid(),
    driver_id            uuid not null references auth.users (id) on delete cascade,
    date                 date not null,
    category             text not null,
    amount               numeric(10, 2) not null,
    deductible           boolean not null default true,
    -- Stored per row because 2026 splits mid-year: 72.5c through Jun 30,
    -- 76c from Jul 1. A single annual rate applied to 2026 is wrong.
    mileage_rate_applied numeric(5, 3),
    note                 text,
    created_at           timestamptz not null default now()
);
create index expenses_driver_date on expenses (driver_id, date desc);

-- --------------------------------------------------------------------------
-- Row-level security — every table, keyed to driver_id
-- --------------------------------------------------------------------------
alter table sessions    enable row level security;
alter table merchants   enable row level security;
alter table buildings   enable row level security;
alter table offers      enable row level security;
alter table deliveries  enable row level security;
alter table zone_stats  enable row level security;
alter table policy_runs enable row level security;
alter table expenses    enable row level security;
alter table parking_risk enable row level security;

do $$
declare t text;
begin
    foreach t in array array['sessions', 'merchants', 'buildings', 'offers',
                             'deliveries', 'zone_stats', 'policy_runs', 'expenses']
    loop
        execute format($f$
            create policy %1$s_owner on %1$s
                for all
                using (driver_id = (select auth.uid()))
                with check (driver_id = (select auth.uid()));
        $f$, t);
    end loop;
end $$;

-- Parking risk is reference data: readable by any authenticated driver,
-- writable only by the service role that ingests municipal sources.
create policy parking_risk_read on parking_risk
    for select using (auth.role() = 'authenticated');

-- The federated layer (F14): shared building intel is readable by every
-- driver, but only rows their owner opted in and that cleared the class
-- guard above. Gate codes are excluded at the view level as well as by the
-- insert trigger — two independent barriers, because one is a typo away
-- from leaking someone's door code.
create view shared_buildings
with (security_invoker = true) as
    select id, h3_res9, label, destination_class, access_notes, entry_door,
           elevator_bank, lobby_handoff_ok, dock_access, safe_park_geom,
           friction_minutes_p50, sample_n
    from buildings
    where shareable;

create policy buildings_shared_read on buildings
    for select using (shareable or driver_id = (select auth.uid()));
