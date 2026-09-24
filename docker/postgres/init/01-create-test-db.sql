-- Runs once, when the Postgres volume is first created.
-- A separate database for integration tests, so running tests never
-- touches your real development data.
CREATE DATABASE kharcha_test OWNER kharcha;
