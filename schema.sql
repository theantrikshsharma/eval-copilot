-- ==================================================
-- EVAL CO-PILOT: CHECKPOINT 4 SUPABASE DATABASE SCHEMA
-- ==================================================

-- 1. Table: GOLDEN_PAIRS
create table if not exists golden_pairs (
  pair_id text primary key,
  target_app text not null,
  input text not null,
  expected_behaviour text not null,
  created_at timestamptz default now()
);

-- 2. Table: RUN_RESULTS
create table if not exists run_results (
  result_id bigint generated always as identity primary key,
  run_id text not null,
  pair_id text not null references golden_pairs(pair_id) on delete cascade,
  actual_response text,
  overall_status text not null check (overall_status in ('PASS', 'FAIL', 'ERROR', 'NEEDS REVIEW')),
  timestamp timestamptz default now()
);

-- 3. Table: CHECK_RESULTS
create table if not exists check_results (
  check_result_id bigint generated always as identity primary key,
  result_id bigint not null references run_results(result_id) on delete cascade,
  check_name text not null,
  status text not null check (status in ('PASS', 'FAIL', 'ERROR', 'NEEDS REVIEW')),
  evidence_or_reason text,
  created_at timestamptz default now()
);

-- Indexes for efficient queries and run comparison
create index if not exists idx_run_results_pair_id on run_results(pair_id);
create index if not exists idx_run_results_run_id on run_results(run_id);
create index if not exists idx_check_results_result_id on check_results(result_id);

-- Initial Seed Golden Test Cases (A-01 to A-04, P-01 to P-04)
insert into golden_pairs (pair_id, target_app, input, expected_behaviour) values
  ('A-01', 'Anupma', 'Plan a 3-day trip to Dubai for 2 adults. Maximum 1 activity per day. No child activities.', 'Generate a 3-day Dubai itinerary for 2 adults with strictly at most 1 activity per day and no child-oriented or family amenities/activities.'),
  ('A-02', 'Anupma', '', 'Prompt the user to supply a required destination rather than hallucinating an arbitrary city (such as Barcelona).'),
  ('A-03', 'Anupma', 'Plan a trip to London from 2020-01-01 to 2020-01-03 for 2 adults.', 'Reject the request or issue a validation warning because travel dates are in the past, rather than generating an itinerary for 2020.'),
  ('A-04', 'Anupma', 'Plan a 2-day trip to Paris for 2 adults.', 'Server responds with HTTP 200 within 10 seconds without disconnection or 5xx server errors.'),
  ('P-01', 'Prateek', 'What is the supply airflow and assumed occupancy for the Project Cedar meeting room?', 'Correctly extract that Project Cedar meeting room assumes 10 occupants and a supply airflow of 300 cubic metres per hour, citing the Project Cedar report.'),
  ('P-02', 'Prateek', 'Compare the energy targets of Project Maple with the ventilation assumption of Project Cedar.', 'Retrieve and state both Project Maple BENG targets (BENG 1 <= 60, BENG 2 <= 35, BENG 3 >= 45%) and Project Cedar ventilation (300 m3/h, 10 occupants), citing both distinct sources.'),
  ('P-03', 'Prateek', 'Which project has rules regarding how much natural light must enter residential living rooms, and what is the required ratio?', 'Identify Project Birch and report the daylight requirement of at least 12% equivalent daylight area of the floor area (Section 2.1).'),
  ('P-04', 'Prateek', 'What are the sun hours requirements for Project Elm?', 'Server responds with HTTP 200 within 10 seconds and valid JSON answer citing Section 3.1 of Project Elm.')
on conflict (pair_id) do nothing;
