-- Extra database for the integration tests (TEST_DATABASE_URL in .env.example). It is wiped
-- by every test run, so it is kept separate from the `metering` database the API uses.
CREATE DATABASE metering_test OWNER metering;
