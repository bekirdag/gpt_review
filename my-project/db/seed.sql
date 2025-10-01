INSERT INTO sessions (id, repo, instructions_path, mode, model, run_command, status)
VALUES
  ('sess-demo-001', '/repos/example', '/repos/example/instructions.txt', 'api', 'gpt-5-codex', 'pytest -q', 'running');

INSERT INTO command_runs (session_id, command, exit_code, duration_seconds, output_tail, triggered_by_patch)
VALUES
  ('sess-demo-001', 'pytest -q', 1, 42.5, 'E   AssertionError: expected 200 got 500', 1);

INSERT INTO logs (session_id, level, message, context)
VALUES
  ('sess-demo-001', 'INFO', 'Blueprint documents generated', '{"paths": [".gpt-review/blueprints/WHITEPAPER.md"]}');
