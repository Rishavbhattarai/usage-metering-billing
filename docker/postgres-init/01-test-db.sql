-- Extra databases on the same server:
--   jobq            Project 1's job queue (its own schema, managed by its own migrations)
--   metering_test   integration tests (TEST_DATABASE_URL); wiped by every test run
--   metering_replay scratch target for `metering replay-check`; wiped by every run
CREATE DATABASE jobq OWNER metering;
CREATE DATABASE metering_test OWNER metering;
CREATE DATABASE metering_replay OWNER metering;
